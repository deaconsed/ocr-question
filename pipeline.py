import threading
import queue
import os
from frame_extractor import process_video
from gpt_extractor import process_single_question

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
            if question_id is None:
                break
            
            if self.stop_requested:
                self.emit_log(f"  [AI] Skipping extraction for Q-ID {question_id} (Stop Requested)")
                self.gpt_queue.task_done()
                continue
                
            try:
                process_single_question(question_id, self.emit_log)
            except Exception as e:
                self.emit_log(f"  [Worker Error] {e}")
            self.gpt_queue.task_done()
            
    def stop_pipeline(self):
        if self.is_running:
            self.stop_requested = True
            self.emit_log("=== STOP SIGNAL RECEIVED. Aborting gracefully... ===")

    def run_pipeline(self, video_path, year):
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
                
            process_video(video_path, self.emit_log, self.gpt_queue, year, check_stop)
            
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
        finally:
            self.is_running = False
            self.current_video = None
            self.gpt_queue.put(None) # Tell worker to stop

# Global instance
pipeline = PipelineOrchestrator()
