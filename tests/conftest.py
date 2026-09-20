"""pytest 环境配置：在导入应用之前指定临时 SQLite 数据库。"""

import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="freeze-thaw-test-")
_db_path = os.path.join(_tmp_dir, "test.db")

os.environ.setdefault("DATABASE_PATH", _db_path)
os.environ.setdefault("FREEZE_THRESHOLD", "-20")
os.environ.setdefault("THAW_THRESHOLD", "0")
os.environ.setdefault("INITIAL_STATE", "thawed")
os.environ.setdefault("MIN_ALLOWED_TEMP", "-80")
os.environ.setdefault("MAX_ALLOWED_TEMP", "10")
os.environ.setdefault("MAX_FREEZE_THAW_CYCLES", "")  # 默认不限制循环上限
