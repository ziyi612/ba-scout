"""Keep game-log indexing off the UI thread and draw only the active roster."""
import logging
from queue import Empty, Queue
from threading import Thread

from .encounters import EncounterHistory


def encounter_label(counts, *, detailed=False):
    parts = []
    for key, label in (("enemy", "曾为对手"), ("ally", "曾为队友")):
        count = counts.get(key, 0)
        if count:
            parts.append(f"{label} {count} 场" if detailed else label)
    return " · ".join(parts)


class EncounterTracker:
    def __init__(self, database, self_id, log_dir):
        self.database = database
        self.self_id = self_id
        self.log_dir = log_dir
        self.jobs = Queue()
        self.results = Queue()
        self.key = None
        self.counts = {}
        self.thread = Thread(target=self._run, name="encounter-history", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            history = EncounterHistory(self.database, self.self_id)
        except Exception:
            logging.exception("Could not open local encounter history")
            return
        try:
            history.import_logs(self.log_dir)
        except Exception:
            logging.exception("Could not import previous game logs")
        while True:
            op, value = self.jobs.get()
            if op == "close":
                return
            try:
                if op == "record":
                    history.record(value)
                elif op == "lookup" and value == self.key:
                    fight, before, ids = value
                    self.results.put((value, history.counts(ids, fight, before=before)))
            except Exception:
                logging.exception("Encounter history failed during %s", op)

    def record(self, roster):
        self.jobs.put(("record", roster))

    def select(self, roster, *, before=None, replay=False):
        key = None
        if roster and roster.players and (not replay or roster.log_timestamp):
            key = (roster.fight_id, roster.log_timestamp or before,
                   tuple(sorted({p["id"] for p in roster.players})))
        if self.key == key:
            return
        previous, self.key = self.key, key
        if previous and key and previous[:2] == key[:2]:
            self.counts = {pid: hits for pid, hits in self.counts.items() if pid in key[2]}
        else:
            self.counts = {}
        if key is not None:
            self.jobs.put(("lookup", key))

    def poll(self):
        changed = False
        while True:
            try:
                key, counts = self.results.get_nowait()
            except Empty:
                return changed
            if self.key == key and self.counts != counts:
                self.counts = counts
                changed = True

    def close(self):
        self.key = None
        self.jobs.put(("close", None))
