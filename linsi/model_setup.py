"""Prepare the public Whisper model in a cancellable, owned child process."""
import os
import sys
import shutil
from pathlib import Path

if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY","1")
    os.environ.setdefault("HF_HUB_DISABLE_XET","1")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT","10")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT","30")
    from faster_whisper import WhisperModel
    directory=Path(sys.argv[1])
    local=directory/'small-local'
    if (local/'model.bin').is_file():
        WhisperModel(str(local),device="cpu",compute_type="int8")
    else:
        from huggingface_hub import snapshot_download
        try:
            cached=Path(snapshot_download('Systran/faster-whisper-small',local_files_only=True))
            names=['config.json','model.bin','tokenizer.json','vocabulary.txt']
            if not all((cached/name).is_file() and (cached/name).stat().st_size for name in names):raise FileNotFoundError()
        except Exception:
            WhisperModel("small",device="cpu",compute_type="int8",download_root=str(directory))
        else:
            local.mkdir(parents=True,exist_ok=True)
            for name in names:
                target=local/(name+'.preparing')
                shutil.copyfile(cached/name,target)
                os.replace(target,local/name)
            WhisperModel(str(local),device="cpu",compute_type="int8")
