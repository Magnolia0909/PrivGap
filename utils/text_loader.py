import os

def load_texts_from_dir(dir_path: str, app_id: str | None = None):
    data = {}
    if not os.path.isdir(dir_path):
        return data

    if app_id:
        fname = f"{app_id}.txt"
        fpath = os.path.join(dir_path, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            return data
        if text:
            data[app_id.strip()] = text
        return data

    for fname in os.listdir(dir_path):
        if not fname.endswith(".txt"):
            continue
        file_app_id = os.path.splitext(fname)[0].strip()
        if not file_app_id:
            continue
        fpath = os.path.join(dir_path, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            continue
        if text:
            data[file_app_id] = text
    return data
