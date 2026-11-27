    if command == "/admin":
        admin_id = get_admin_chat_id()
        if admin_id is None or str(chat_id) != str(admin_id):
            message = "این دستور فقط برای مدیر ربات فعال است."
        else:
            stats = db_queue_stats()
            with _service_lock:
                alive_workers = sum(worker.is_alive() for worker in _worker_threads)
            with _poll_health_lock:
                last_ok = _last_poll_success_at
                poll_error = _last_poll_error_type
            age = round(time.time() - last_ok, 1) if last_ok is not None else "نامشخص"
            runtime_label = "فعال" if _runtime_active else "غیرفعال"
            message = (
                "🛠 پنل مدیریتی Nepton\n"
                f"نسخه: {VERSION}\n"
                f"وضعیت Runtime: {runtime_label}\n"
                f"Worker فعال: {alive_workers}/{MAX_PARALLEL_CHATS}\n"
                f"آخرین Poll موفق: {age} ثانیه قبل\n"
                f"خطای آخر Poll: {poll_error or 'ندارد'}\n"
                f"پیام‌های صف: {stats['queued_messages']} (در انتظار: {stats['pending_messages']})\n"
                f"در حال پردازش: {stats['processing_messages']}\n"
                f"Dead-letter: {stats['dead_letters']}\n"
                f"قدیمی‌ترین پیام صف: {stats['oldest_queue_age_seconds']} ثانیه"
            )
        return {
            "responses": [(chat_id, message)], "history_user": None,
            "history_assistant": None, "clear_history": False, "feedback_action": None,
        }

    if command == "/admin":
        admin_id = get_admin_chat_id()
        if admin_id is None or str(chat_id) != str(admin_id):
            message = "این دستور فقط برای مدیر ربات فعال است."
            markup = None
        else:
            message = "🛠 پنل مدیریتی Nepton\nبرای شروع یا پایان گفتگو از دکمه‌ها استفاده کن."
            markup = {"keyboard": [[{"text": "💬 شروع گفتگو"}, {"text": "⏹ پایان گفتگو"}]], "resize_keyboard": True, "one_time_keyboard": False}
        return {"responses": [(chat_id, message)], "history_user": None, "history_assistant": None, "clear_history": False, "feedback_action": None, "reply_markup": markup}

    if command == "/market":
        result = get_live_market_prices()
        return {
            "responses": [(chat_id, result)], "history_user": None,
            "history_assistant": None, "clear_history": False, "feedback_action": None,
        }

    if command == "/search":
        query = text.partition(" ")[2].strip()
        if not query:
            return {
                "responses": [(chat_id, "عبارت جست‌وجو را بعد از دستور بنویس؛ مثال: /search آخرین اخبار فناوری")],
                "history_user": None, "history_assistant": None,
                "clear_history": False, "feedback_action": None,
            }
        result = get_live_web_results(query)
        return {
            "responses": [(chat_id, result)], "history_user": query,
            "history_assistant": result, "clear_history": False, "feedback_action": None,
        }

    if text.strip() == "🌦 هواشناسی" or command == "/weather":
        expires_at = int(time.time()) + 300
        db_set_state(weather_state_key, str(expires_at))
        return {
            "responses": [(chat_id, "نام شهر ایران را بفرست؛ مثلاً: مشهد یا کاشمر\nبرای لغو، /cancel را بفرست. این درخواست تا ۵ دقیقه فعال می‌ماند.")],
            "history_user": None, "history_assistant": None,
            "clear_history": False, "feedback_action": None,
        }

    if command == "/cancel":
        try:
            weather_cancel_active = int(weather_pending_until or 0) > int(time.time())
        except (TypeError, ValueError):
            weather_cancel_active = False

        if weather_cancel_active:
            db_delete_state(weather_state_key)
            return {
                "responses": [(chat_id, "درخواست هواشناسی لغو شد.")],
                "history_user": None, "history_assistant": None,
                "clear_history": False, "feedback_action": None,
            }

        if weather_pending_until is not None:
            db_delete_state(weather_state_key)

    if weather_pending_until is not None and not text.startswith("/"):
        try:
            pending_active = int(weather_pending_until) > int(time.time())
        except (TypeError, ValueError):
            pending_active = False

        if pending_active:
            try:
                weather_result = get_live_weather("هواشناسی " + text.strip())
            except UserVisibleError as exc:
                weather_result = str(exc)
            else:
                # Keep the pending state on transient errors so the durable queue
                # can retry the same update without losing the requested city flow.
                db_delete_state(weather_state_key)

            return {
                "responses": [(chat_id, weather_result)],
                "history_user": None, "history_assistant": None,
                "clear_history": False, "feedback_action": None,
            }

        db_delete_state(weather_state_key)
    if command in {"/start", "/help"}:
        message = START_MESSAGE if command == "/start" else HELP_MESSAGE
        return {
            "responses": [(chat_id, message)], "history_user": None,
            "history_assistant": None, "clear_history": False, "feedback_action": None,
            "reply_markup": {
                "keyboard": [[{"text": "🌦 هواشناسی"}, {"text": "💰 قیمت بازار"}]],
                "resize_keyboard": True,
                "one_time_keyboard": False,
            } if command == "/start" else None,
        }
    if command == "/reset":
        return {
            "responses": [(chat_id, "حافظه گفت‌وگوی شما پاک شد.")],
            "history_user": None, "history_assistant": None,
            "clear_history": True, "feedback_action": None,
        }

    # Global navigation commands must remain usable while feedback is pending.

    feedback_result = process_feedback_message(chat_id, text)
    if feedback_result is not None:
        return {
            "responses": feedback_result["responses"],
            "history_user": None, "history_assistant": None,
            "clear_history": False,
            "feedback_action": feedback_result.get("feedback_action"),
        }

    if is_creator_question(text):
        return {
            "responses": [(chat_id, CREATOR_ANSWER)],
            "history_user": text, "history_assistant": CREATOR_ANSWER,
            "clear_history": False, "feedback_action": None,
        }

    fixed_answer = get_fixed_conversation_answer(text)
    if fixed_answer is not None:
        return {
            "responses": [(chat_id, fixed_answer)],
            "history_user": text, "history_assistant": fixed_answer,
            "clear_history": False, "feedback_action": None,
        }

    bounded_text = text[:MAX_MESSAGE_LENGTH]
    if len(text) > MAX_MESSAGE_LENGTH:
        logger.info("Incoming message truncated chars=%s limit=%s", len(text), MAX_MESSAGE_LENGTH)
    try:
        answer = call_ai(chat_id, bounded_text)
        history_user, history_assistant = bounded_text, answer
        answer = f"✦ NEPTON · پاسخ هوشمند\n━━━━━━━━━━━━━━━━━━\n{answer}"
    except UserVisibleError as exc:
        answer = str(exc)
        history_user = history_assistant = None

    logger.info("TIMING build_response_plan_ms=%.0f", (time.perf_counter() - started) * 1000)
    responses = [(chat_id, answer)]
    if db_get_state(f"talk_active_{chat_id}") and re.search(r"(?:کصخل|کسخل|کصکش|کسکش|حرومزاده|بی‌شعور|بی شعور|جاکش|مادرجنده|fuck|shit|asshole)", text, re.IGNORECASE):
        admin_id = get_admin_chat_id()
        if admin_id is not None:
            report = f"⚠️ گزارش بی‌احترامی\nشناسه: {chat_id}\nمتن: {text[:1500]}\nزمان: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC"
            responses.append((admin_id, report))
    return {
        "responses": responses,
        "history_user": history_user,
        "history_assistant": history_assistant,
        "clear_history": False,
        "feedback_action": None,
    }


def flatten_response_plan(responses, reply_markup=None):
    """Convert responses into durable, individually acknowledged chunks."""
    plan = []
    for chat_id, text in responses:
        for chunk in split_message(text):
            item = {"chat_id": str(chat_id), "text": chunk}
            if reply_markup is not None:
                item["reply_markup"] = reply_markup
            plan.append(item)
    return plan


# =========================================================
# Queue workers
# =========================================================
def _process_claimed_item_impl(item, worker_id):
    message_id = item["id"]
    queue_age_ms = max(0.0, (time.time() - float(item.get("queued_at") or time.time())) * 1000)
    logger.info("TIMING queue_wait_ms=%.0f", queue_age_ms)

    response_plan_raw = item.get("response_plan")
    if not response_plan_raw:
        result = build_response_plan(item["chat_id"], item["message_text"])
        plan = flatten_response_plan(
            result["responses"], reply_markup=result.get("reply_markup")
        )
        saved = db_prepare_response_plan(
            message_id, worker_id, plan,
            history_user=result.get("history_user"),
            history_assistant=result.get("history_assistant"),
            clear_history=result.get("clear_history", False),
            feedback_action=result.get("feedback_action"),
        )
        if not saved:
            raise RuntimeError("Could not persist response plan or worker lease was lost")
        response_index = 0
    else:
        try:
            plan = json.loads(response_plan_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Stored response plan is invalid JSON") from exc
        response_index = int(item.get("response_index") or 0)

    if not isinstance(plan, list) or response_index < 0 or response_index > len(plan):
        raise RuntimeError("Stored response plan or response cursor is invalid")

    while response_index < len(plan):
        if not db_renew_lease(message_id, worker_id):
            raise RuntimeError("Worker lease was lost before message delivery")
        part = plan[response_index]
        if (
            not isinstance(part, dict)
            or not part.get("chat_id")
            or not isinstance(part.get("text"), str)
            or not part["text"]
        ):
            raise RuntimeError("Stored response plan contains an invalid item")

        send_one_message(
            part["chat_id"], part["text"], reply_markup=part.get("reply_markup")
        )
        if not db_advance_response(message_id, worker_id, response_index):
            raise RuntimeError("Could not advance durable response cursor")
        response_index += 1

    if not db_finish_message(message_id, worker_id):
        raise RuntimeError("Could not finalize queue item")

    total_ms = max(
        0.0, (time.time() - float(item.get("queued_at") or time.time())) * 1000
    )
    logger.info("TIMING queue_total_ms=%.0f", total_ms)
    if SENTRY_ENABLED:
        sentry_metrics.count("neptonbot.messages.processed", 1)
        sentry_metrics.distribution("neptonbot.queue.total_ms", total_ms)
        sentry_metrics.distribution("neptonbot.queue.wait_ms", queue_age_ms)


def _process_claimed_item(item, worker_id):
    if not SENTRY_ENABLED:
        return _process_claimed_item_impl(item, worker_id)
    with sentry_sdk.start_transaction(
        op="task",
        name="neptonbot.process_message",
    ) as transaction:
        transaction.set_tag("component", "queue_worker")
        transaction.set_tag("worker", threading.current_thread().name)
        return _process_claimed_item_impl(item, worker_id)


def chat_worker():
    worker_id = _worker_id()
    logger.info("Worker started id=%s", worker_id)

    while not _polling_stop.is_set() and not _runtime_lease_lost.is_set():
        item = None
        try:
            item = db_claim_next_message(worker_id)
            if item is None:
                _queue_wakeup.wait(WORKER_IDLE_WAIT)
                _queue_wakeup.clear()
                continue
            _process_claimed_item(item, worker_id)

        except Exception as exc:
            if isinstance(exc, (TransientAPIError, PermanentAPIError)):
                logger.warning("Worker request failed type=%s", type(exc).__name__)
            else:
                logger.exception("Worker processing failed type=%s", type(exc).__name__)

            if item is not None:
                try:
                    attempts_done = int(item.get("attempts", 0)) + 1
                    permanent = isinstance(exc, PermanentAPIError)
                    if permanent or attempts_done > MAX_WORKER_RETRIES:
                        failed = db_dead_letter(item["id"], worker_id, str(exc))
                        if failed is not None:
                            try:
                                send_one_message(
                                    failed["chat_id"],
                                    "متأسفانه پردازش پیام به دلیل یک خطای فنی ناموفق ماند. "
                                    "لطفاً دوباره پیام بده.",
                                )
                            except Exception:
                                logger.warning("Could not send final failure notice")
                    else:
                        db_retry_message(
                            item["id"], worker_id, attempts_done, str(exc),
                            retry_after=getattr(exc, "retry_after", None),
                        )
                except Exception:
                    logger.exception("Could not update failed queue item")
            _polling_stop.wait(0.1)

    logger.info("Worker stopped id=%s", worker_id)


def _start_or_replace_workers():
    global _worker_threads
    with _service_lock:
        _worker_threads = [worker for worker in _worker_threads if worker.is_alive()]
        missing = max(0, MAX_PARALLEL_CHATS - len(_worker_threads))
        for _ in range(missing):
            worker = threading.Thread(
                target=chat_worker,
                name=f"NeptonWorker-{len(_worker_threads) + 1}",
                daemon=True,
            )
            worker.start()
            _worker_threads.append(worker)
        if missing:
            logger.info("Started %s queue worker(s)", missing)


# =========================================================
# Polling and update batch handling
# =========================================================
def prepare_update_batch(updates):
    prepared = []
    last_update_id = None

    for update in updates:
        update_id = get_update_id(update)
        if update_id is not None:
            last_update_id = (
                update_id if last_update_id is None
                else max(last_update_id, update_id)
            )

        extracted = extract_message(update)
        if extracted is not None:
            chat_id, message_text = extracted
            prepared.append((update_id, chat_id, message_text))

    return prepared, last_update_id


def polling_loop():
    offset = db_get_offset()
    backoff = POLL_ERROR_BACKOFF_START
    idle_wait = POLL_EMPTY_WAIT
    last_cleanup = time.monotonic()
    logger.info("Soroush polling started offset_present=%s", offset is not None)

    while not _polling_stop.is_set() and not _runtime_lease_lost.is_set():
        try:
            if time.monotonic() - last_cleanup >= DB_CLEANUP_INTERVAL:
                try:
                    db_cleanup_old_records()
                except Exception:
                    logger.exception("Database cleanup failed")
                last_cleanup = time.monotonic()

            payload = {"offset": offset} if offset is not None else {}
            started = time.perf_counter()
            data = soroush_request("getUpdates", payload)
            logger.debug("TIMING get_updates_ms=%.0f", (time.perf_counter() - started) * 1000)

            if _runtime_lease_lost.is_set():
                break
            if not has_update_container(data):
                raise TransientAPIError("Unexpected Soroush getUpdates response")

            _record_poll_success()
            updates = extract_updates(data)
            if not updates:
                backoff = POLL_ERROR_BACKOFF_START
                _polling_stop.wait(idle_wait)
                idle_wait = min(idle_wait * 1.35, POLL_EMPTY_WAIT_MAX)
                continue

            idle_wait = POLL_EMPTY_WAIT
            prepared, last_id = prepare_update_batch(updates)
            next_offset = last_id + 1 if last_id is not None else None
            if not db_enqueue_updates(prepared, next_offset):
                _polling_stop.wait(POLL_ERROR_BACKOFF_START)
                continue

            if next_offset is not None:
                offset = next_offset
            backoff = POLL_ERROR_BACKOFF_START

        except TransientAPIError as exc:
            _record_poll_error(exc)
            wait = max(backoff, float(getattr(exc, "retry_after", 0) or 0))
            wait = min(wait + random.uniform(0, min(wait * 0.15, 1.0)), POLL_ERROR_BACKOFF_MAX)
            logger.warning(
                "Polling temporarily failed type=%s retry_in=%.1fs",
                type(exc).__name__, wait,
            )
            _polling_stop.wait(wait)
            backoff = min(backoff * 2, POLL_ERROR_BACKOFF_MAX)
        except PermanentAPIError as exc:
            _record_poll_error(exc)
            logger.error("Polling request rejected type=%s reason=%s", type(exc).__name__, str(exc)[:180])
            _polling_stop.wait(backoff)
            backoff = min(backoff * 2, POLL_ERROR_BACKOFF_MAX)
        except Exception as exc:
            _record_poll_error(exc)
            logger.exception("Polling iteration failed")
            _polling_stop.wait(backoff)
            backoff = min(backoff * 2, POLL_ERROR_BACKOFF_MAX)

    logger.info("Soroush polling stopped")


def _polling_main():
    try:
        polling_loop()
    except Exception:
        logger.exception("Polling thread stopped unexpectedly")


def start_polling():
    global _polling_thread
    with _service_lock:
        if _polling_thread is not None and _polling_thread.is_alive():
            return
        _polling_thread = threading.Thread(
            target=_polling_main,
            name="SoroushPolling",
            daemon=True,
        )
        _polling_thread.start()
        logger.info("Polling thread started")


def _start_runtime_acquirer():
    global _runtime_acquire_thread
    with _runtime_acquire_thread_lock:
        if _runtime_acquire_thread is not None and _runtime_acquire_thread.is_alive():
            return
        _runtime_acquire_thread = threading.Thread(
            target=_runtime_acquisition_loop,
            name="NeptonRuntimeAcquirer",
            daemon=True,
        )
        _runtime_acquire_thread.start()


def _runtime_acquisition_loop():
    logger.info("Runtime acquisition retry loop started")
    while not _runtime_active:
        time.sleep(RUNTIME_ACQUIRE_RETRY_INTERVAL)
        if _runtime_active:
            break
        try:
            if start_runtime_services():
                logger.info("Runtime ownership acquired after retry")
                return
        except Exception:
            logger.exception("Runtime acquisition retry failed")
    logger.info("Runtime acquisition retry loop stopped")


def runtime_supervisor():
    logger.info("Runtime supervisor started")
    while True:
        if _runtime_lease_lost.is_set():
            logger.error("Runtime supervisor detected lease loss; handing over ownership")
            shutdown_runtime()
            _start_runtime_acquirer()
            return
        if _polling_stop.wait(SUPERVISOR_INTERVAL):
            break
        try:
            _start_or_replace_workers()
            start_polling()
        except Exception:
            logger.exception("Runtime supervisor iteration failed")
    logger.info("Runtime supervisor stopped")


def start_runtime_services():
    global _runtime_active, _supervisor_thread, _runtime_heartbeat_thread
    with _runtime_start_lock:
        if _runtime_active:
            return True
        if not acquire_runtime_lock():
            return False

        try:
            init_database()
            db_recover_interrupted_jobs()
            load_persistent_runtime_state()
            register_bot_commands()

            _polling_stop.clear()
            _queue_wakeup.clear()
            _runtime_lease_lost.clear()

            if USE_POSTGRES:
                _runtime_heartbeat_thread = threading.Thread(
                    target=runtime_lease_heartbeat,
                    name="NeptonLeaseHeartbeat",
                    daemon=True,
                )
                _runtime_heartbeat_thread.start()

            _start_or_replace_workers()
            start_polling()

            _supervisor_thread = threading.Thread(
                target=runtime_supervisor,
                name="NeptonSupervisor",
                daemon=True,
            )
            _supervisor_thread.start()
            _runtime_active = True
            logger.info("NeptonBot %s runtime services started", VERSION)
            return True
        except Exception:
            logger.exception("Runtime startup failed")
            shutdown_runtime()
            raise


def shutdown_runtime():
    global _runtime_active
    _polling_stop.set()
    _queue_wakeup.set()
    current = threading.current_thread()
    with _service_lock:
        worker_snapshot = list(_worker_threads)
    threads = [
        thread for thread in [
            _polling_thread,
            _supervisor_thread,
            _runtime_heartbeat_thread,
            *worker_snapshot,
        ]
        if thread is not None and thread is not current and thread.is_alive()
    ]
    deadline = time.monotonic() + 8.0
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    alive = [thread.name for thread in threads if thread.is_alive()]
    if alive:
        logger.warning(
            "Threads still active during shutdown: %s; runtime lock stays held until process exit",
            ", ".join(alive),
        )
    else:
        _release_runtime_lock()
    _runtime_active = False
    logger.info("NeptonBot shutdown completed")


atexit.register(close_database_pool)
atexit.register(shutdown_runtime)


# =========================================================
# Startup
# =========================================================
if os.getenv("NEPTON_DISABLE_AUTOSTART", "0").strip().lower() not in {"1", "true", "yes"}:
    if not start_runtime_services():
        _start_runtime_acquirer()
else:
    logger.info("NEPTON_DISABLE_AUTOSTART is enabled; runtime will start in the post-worker hook")

logger.info("NeptonBot %s module initialized", VERSION)