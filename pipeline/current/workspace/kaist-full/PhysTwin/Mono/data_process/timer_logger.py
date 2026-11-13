import time
import logging

class TimerLogger:
    def __init__(self, logger_name="TimerLogger"):
        self.logger = logging.getLogger(logger_name)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter("[%(levelname)s] %(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

        self.start_times = {}
        self.elapsed_times = {}

    def start(self, tag: str):
        if tag in self.start_times:
            self.logger.warning(f"Tag '{tag}' is already running.")
            return

        self.start_times[tag] = time.time()
        self.logger.info(f"Start {tag}")

    def end(self, tag: str):
        if tag not in self.start_times:
            self.logger.warning(f"End called for '{tag}' but no start record found.")
            return

        elapsed = time.time() - self.start_times[tag]

        if tag not in self.elapsed_times:
            self.elapsed_times[tag] = elapsed
        else:
            self.elapsed_times[tag] += elapsed

        self.logger.info(f"End {tag} (+{elapsed:.3f}s, total {self.elapsed_times[tag]:.3f}s)")

        del self.start_times[tag]

    @staticmethod
    def format_elapsed(seconds: float):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        if h > 0:
            return f"{h}h {m}min {s:.4f}s"
        elif m > 0:
            return f"{m}min {s:.4f}s"
        else:
            return f"{s:.4f}s"

    def write_to_file(self, filename: str):
        with open(filename, "w") as f:
            for tag, sec in self.elapsed_times.items():
                f.write(f"{tag}: {self.format_elapsed(sec)}\n")

        self.logger.info(f"Time log saved to {filename}")
