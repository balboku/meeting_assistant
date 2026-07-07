"""
gui/recorder.py - 負責封裝 sounddevice 錄音邏輯
"""
import queue
import sys
import sounddevice as sd
import soundfile as sf
import threading

class AudioRecorder:
    def __init__(self, samplerate=44100, channels=1):
        self.samplerate = samplerate
        self.channels = channels
        self.q = queue.Queue()
        self.is_recording = False
        self._stream = None
        self._file = None
        self._thread = None
        self.filename = None

    def _callback(self, indata, frames, time, status):
        """此 callback 會在獨立的 audio thread 被呼叫"""
        if status:
            print(status, file=sys.stderr)
        if self.is_recording:
            self.q.put(indata.copy())

    def _write_thread(self):
        """將音訊寫入檔案的獨立執行緒，避免阻塞 callback"""
        with sf.SoundFile(self.filename, mode='x', samplerate=self.samplerate,
                          channels=self.channels, subtype='PCM_24') as file:
            while self.is_recording or not self.q.empty():
                try:
                    # 設定 timeout，避免停止錄音時卡死
                    data = self.q.get(timeout=0.1)
                    file.write(data)
                except queue.Empty:
                    pass

    def start_recording(self, filename):
        if self.is_recording:
            return

        self.filename = filename
        self.is_recording = True
        self.q = queue.Queue()  # 清空 queue

        # 啟動寫入檔案的 thread
        self._thread = threading.Thread(target=self._write_thread)
        self._thread.start()

        # 啟動 sounddevice InputStream
        self._stream = sd.InputStream(samplerate=self.samplerate, channels=self.channels,
                                      callback=self._callback)
        self._stream.start()

    def stop_recording(self):
        if not self.is_recording:
            return

        self.is_recording = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._thread:
            self._thread.join()
            self._thread = None
