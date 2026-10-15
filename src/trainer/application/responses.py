from threading import RLock, Thread

from trainer.application.ports import (
    LocalStorage,
    ResponseAttempt,
    ResponseControl,
    TrainerEvent,
    TrainerRuntime,
)
from trainer.application.trainer_response import RespondToDiscussion


class DiscussionResponses:
    """Own execution independently of browser connections; SQLite owns the visible log."""

    def __init__(self, storage: LocalStorage, runtime: TrainerRuntime) -> None:
        self.storage = storage
        self.runtime = runtime
        self.lock = RLock()
        self._workers: dict[int, Thread] = {}
        self._keys: dict[int, tuple[str, str, int]] = {}
        self._controls: dict[int, ResponseControl] = {}

    def start(
        self, origin: str, origin_id: str, base_ply: int,
        retry_of: int | None = None,
    ) -> ResponseAttempt:
        with self.lock:
            attempt = self.storage.begin_response(origin, origin_id, base_ply, retry_of)
            key = (origin, origin_id, base_ply)
            self._keys[attempt.id] = key
            self._controls[attempt.id] = ResponseControl()
            worker = Thread(target=self._run, args=(attempt, key), daemon=True)
            self._workers[attempt.id] = worker
            worker.start()
            return attempt

    def _run(self, initial: ResponseAttempt, key: tuple[str, str, int]) -> None:
        attempt_id = initial.id
        try:
            for event in RespondToDiscussion(self.storage, self.runtime).stream(
                *key, initial, self._controls[attempt_id],
            ):
                with self.lock:
                    attempt = next(
                        a for a in self.storage.response_attempts(*key)
                        if a.id == attempt_id
                    )
                    if attempt.status != "running":
                        return
                    self.storage.record_response_event(attempt_id, event)
        except Exception:
            with self.lock:
                current = next(
                    (a for a in self.storage.response_attempts(*key) if a.id == attempt_id), None,
                )
                if current is None or current.status != "running":
                    return
                control = self._controls[attempt_id]
                with control.lock:
                    control.cancelled.set()
                    session = self.storage.get_discussion_session(*key)
                    if session is not None:
                        try:
                            self.runtime.abort(session)
                        except Exception:
                            pass
                self.storage.record_response_event(attempt_id, TrainerEvent(
                    "failed", "Trainer response failed. Your Discussion is saved; please try again.",
                ))

    def wait(self, attempt_id: int) -> None:
        self._workers[attempt_id].join()

    def limit_evaluations(self, attempt_id: int, key: tuple[str, str, int]) -> None:
        control = self._controls[attempt_id]
        with control.lock:
            control.cancelled.set()
            self.storage.record_response_event(attempt_id, TrainerEvent(
                "limited", "Trainer reached the limit of 3 Evaluator consultations. Continue with another request.",
            ))
            session = self.storage.get_discussion_session(*key)
            if session is not None:
                try:
                    self.runtime.abort(session)
                except Exception:
                    pass

    def running_key(self, attempt_id: int) -> tuple[str, str, int]:
        key = self._keys[attempt_id]
        if not self.storage.trainer_consent() or not any(
            a.id == attempt_id and a.status == "running"
            for a in self.storage.response_attempts(*key)
        ):
            raise ValueError("Response is no longer active")
        return key

    def interrupt(self, attempt: ResponseAttempt, key: tuple[str, str, int]) -> None:
        with self.lock:
            current = next(
                a for a in self.storage.response_attempts(*key)
                if a.id == attempt.id
            )
            if current.status != "running":
                return
            control = self._controls[attempt.id]
            with control.lock:
                control.cancelled.set()
                session = self.storage.get_discussion_session(*key)
                try:
                    if session is not None:
                        self.runtime.abort(session)
                finally:
                    self.storage.record_response_event(
                        attempt.id, TrainerEvent("interrupted", "Trainer response interrupted."),
                    )

    def shutdown(self) -> None:
        for attempt_id, key in list(self._keys.items()):
            attempt = next(
                (a for a in self.storage.response_attempts(*key) if a.id == attempt_id), None,
            )
            if attempt is not None and attempt.status == "running":
                try:
                    self.interrupt(attempt, key)
                except Exception:
                    self.storage.record_response_event(
                        attempt_id, TrainerEvent("interrupted", "Application stopped during the response."),
                    )
        self.runtime.stop()
        for worker in self._workers.values():
            worker.join()
