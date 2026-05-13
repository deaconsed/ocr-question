import threading
import queue
import os
from frame_extractor import process_video
from folder_importer import import_screenshot_folder
from gpt_extractor import process_single_question
import traceback

class PipelineOrchestrator:
    def __init__(self):
        self.log_queues = []
        self.is_running = False
        self.stop_requested = False
        self.gpt_queue = queue.Queue()
        self.current_video = None

    def emit_log(self, msg):
        print(msg)
        for q in self.log_queues:
            q.put(msg)

    def subscribe_logs(self):
        q = queue.Queue()
        self.log_queues.append(q)
        return q

    def unsubscribe_logs(self, q):
        if q in self.log_queues:
            self.log_queues.remove(q)

    def gpt_worker(self):
        while True:
            question_id = self.gpt_queue.get()
            self.gpt_queue.task_done()  # always signal done, even for the None sentinel
            if question_id is None:
                break

            if self.stop_requested:
                self.emit_log(f"  [AI] Skipping extraction for Q-ID {question_id} (Stop Requested)")
                continue

            try:
                process_single_question(question_id, self.emit_log)
            except Exception as e:
                self.emit_log(f"  [Worker Error] {e}")
                self.emit_log(traceback.format_exc())
            
    def extract_one_async(self, question_id, force=False):
        """
        Fire-and-forget GPT extraction for a single question outside of a
        running pipeline (e.g. after manual review of an unidentified frame).
        The pipeline-bound gpt_worker exits when run_pipeline finishes, so the
        normal queue mechanism doesn't work in idle state — this spawns its
        own daemon thread that calls process_single_question directly.
        """
        def _worker():
            try:
                self.emit_log(f"  [AI] Manual extraction queued for question {question_id} (force={force})")
                process_single_question(question_id, self.emit_log, force=force)
            except Exception as e:
                self.emit_log(f"  [AI Worker Error] q={question_id}: {e}")
                self.emit_log(traceback.format_exc())

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def stop_pipeline(self):
        if self.is_running:
            self.stop_requested = True
            self.emit_log("=== STOP SIGNAL RECEIVED. Aborting gracefully... ===")

    def run_pipeline(self, video_path, year, sessions=None):
        self.is_running = True
        self.stop_requested = False
        self.current_video = os.path.basename(video_path)

        # Start GPT worker thread
        worker_thread = threading.Thread(target=self.gpt_worker, daemon=True)
        worker_thread.start()

        try:
            self.emit_log(f"=== Starting Pipeline for {self.current_video} ===")

            # Pass a lambda to let the extractor check if it should abort
            def check_stop():
                return self.stop_requested

            process_video(video_path, self.emit_log, self.gpt_queue, year, check_stop, sessions)
            
            if self.stop_requested:
                self.emit_log("Frame extraction aborted. Clearing pending GPT items...")
                # Clear queue
                while not self.gpt_queue.empty():
                    self.gpt_queue.get_nowait()
                    self.gpt_queue.task_done()
                self.emit_log("=== Pipeline Stopped ===")
            else:
                self.emit_log("Frame extraction complete. Waiting for GPT worker to finish pending items...")
                self.gpt_queue.join()  # wait for all items to be processed
                self.emit_log("=== Pipeline Complete! ===")
                
        except Exception as e:
            self.emit_log(f"Pipeline Error: {e}")
            self.emit_log(traceback.format_exc())
        finally:
            self.is_running = False
            self.current_video = None
            self.gpt_queue.put(None) # Tell worker to stop

    def run_folder_import(self, folder_path, year, num_workers=8):
        self.is_running = True
        self.stop_requested = False
        self.current_video = os.path.basename(folder_path)

        self.emit_log(f"  Starting {num_workers} parallel GPT workers...")
        workers = []
        for _ in range(num_workers):
            t = threading.Thread(target=self.gpt_worker, daemon=True)
            t.start()
            workers.append(t)

        try:
            def check_stop():
                return self.stop_requested

            import_screenshot_folder(folder_path, year, self.emit_log, self.gpt_queue, check_stop)

            if self.stop_requested:
                self.emit_log("Import aborted. Clearing pending GPT items...")
                while not self.gpt_queue.empty():
                    try:
                        self.gpt_queue.get_nowait()
                        self.gpt_queue.task_done()
                    except Exception:
                        break
                self.emit_log("=== Import Stopped ===")
            else:
                self.emit_log(f"Folder import done. Waiting for {num_workers} GPT workers to finish...")
                self.gpt_queue.join()
                self.emit_log("=== Pipeline Complete! ===")

        except Exception as e:
            self.emit_log(f"Import Error: {e}")
            self.emit_log(traceback.format_exc())
        finally:
            self.is_running = False
            self.current_video = None
            # One None sentinel per worker so every thread exits cleanly
            for _ in range(num_workers):
                self.gpt_queue.put(None)


# Global instance
pipeline = PipelineOrchestrator()
