import csv
import html
import json
import os
import random
import shutil
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional
from PyQt5 import QtCore, QtGui, QtWidgets, QtTextToSpeech

warnings.filterwarnings(
    "ignore",
    message=r"Glyph .* missing from font\\(s\\) .*"
)

# --- stats & viz (optional matplotlib) ---
HAS_MPL = False
try:
    import matplotlib

    matplotlib.use("Qt5Agg")
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    import matplotlib.pyplot as plt

    HAS_MPL = True
except Exception:
    HAS_MPL = False

# --- CJK 字体（多重候选 + 本地字体） ---
CJK_FONT_CANDIDATES = [
    # 优先使用我们注册的 Zen Maru Gothic
    "Zen Maru Gothic", "ZenMaruGothic", "ZenMaruGothic-Medium",
    # JP（含假名）
    "Meiryo", "Yu Gothic UI", "Yu Gothic", "MS Gothic", "MS Mincho",
    "Hiragino Sans", "Hiragino Kaku Gothic ProN", "IPAexGothic",
    # 开源全量 CJK
    "Noto Sans CJK JP", "Noto Serif CJK JP",
    "Noto Sans CJK SC", "Noto Serif CJK SC",
    "Source Han Sans JP", "Source Han Serif JP",
    "Source Han Sans SC", "Source Han Serif SC",
    # 中文（常见）
    "Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "SimSun",
    # 兜底（广覆盖）
    "Arial Unicode MS"
]



def _init_mpl_cjk_fonts(extra_font_paths=None):
    """
    1) 尝试注册本地字体文件（.ttf/.otf）
    2) 构造一个“可用字体名”的候选列表，按顺序作为 sans-serif 回退链
    3) 关闭 unicode 负号问题
    """
    try:
        import glob
        import matplotlib
        from matplotlib import font_manager as fm

        # 1) 先注册本地字体（可选）
        if extra_font_paths:
            for fp in extra_font_paths:
                try:
                    if os.path.isfile(fp) and (fp.lower().endswith(".ttf") or fp.lower().endswith(".otf")):
                        fm.fontManager.addfont(fp)
                except Exception:
                    pass
        # 也支持 ./fonts/*.ttf 的就近加载
        local_fonts_dir = os.path.join(os.path.dirname(__file__), "fonts")
        if os.path.isdir(local_fonts_dir):
            for fp in glob.glob(os.path.join(local_fonts_dir, "*.ttf")) + glob.glob(
                    os.path.join(local_fonts_dir, "*.otf")):
                try:
                    fm.fontManager.addfont(fp)
                except Exception:
                    pass

        # 刷新字体管理器
        try:
            fm._load_fontmanager(try_read_cache=False)
        except Exception:
            pass

        # 2) 构造可用字体名列表（多重 fallback）
        available = {f.name for f in fm.fontManager.ttflist}
        fallback = [name for name in CJK_FONT_CANDIDATES if name in available]
        # 至少留一个常见英文字体，混排更稳
        fallback.append("DejaVu Sans")

        matplotlib.rcParams["font.family"] = "sans-serif"
        matplotlib.rcParams["font.sans-serif"] = fallback
        matplotlib.rcParams["axes.unicode_minus"] = False
        # mathtext 用拉丁即可，避免干扰 CJK
        matplotlib.rcParams["mathtext.fontset"] = "dejavusans"
    except Exception:
        pass


if HAS_MPL:
    # 把 ZenMaruGothic-Medium.ttf 注入 matplotlib 字体管理器（同级或 ./fonts/）
    _init_mpl_cjk_fonts([
        os.path.join(os.path.dirname(__file__), "ZenMaruGothic-Me啊【】dium.ttf"),
        os.path.join(os.path.dirname(__file__), "fonts", "ZenMaruGothic-Medium.ttf"),
    ])


DB_PATH = os.path.join(os.path.expanduser("~"), "vocab_trainer_units_v2.db")

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), "vocab_settings.json")

BACKUP_DIR = os.path.join(os.path.expanduser("~"), "vocab_backups")


def _ensure_dir(p: str):
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass


def backup_db_file(path: str) -> str | None:
    """
    迁移前自动备份数据库到用户目录下的 vocab_backups/ 里。
    返回备份文件路径；失败返回 None。
    """
    try:
        _ensure_dir(BACKUP_DIR)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_DIR, f"{Path(path).stem}_{ts}.db")
        shutil.copy2(path, dst)
        return dst
    except Exception:
        return None


def _has_index(cur: sqlite3.Cursor, name: str) -> bool:
    """返回索引是否已存在（避免 IDE 对 IF NOT EXISTS 的误报）"""
    row = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (name,)
    ).fetchone()
    return row is not None


def ensure_index(cur: sqlite3.Cursor, name: str, create_sql: str) -> None:
    """如果索引不存在才创建；create_sql 不要带 IF NOT EXISTS"""
    if not _has_index(cur, name):
        cur.execute(create_sql)


# --------------------------
# 数据库初始化
# --------------------------
def init_db(path=DB_PATH):
    first = not os.path.exists(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()

    # --- PRAGMA：迁移阶段更稳，运行阶段更快 ---
    try:
        cur.execute('PRAGMA foreign_keys=ON')
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass

    # --- 基础表：cards（已存在则跳过）---
    cur.execute('''
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY,
        language TEXT NOT NULL,
        unit TEXT DEFAULT '',
        term TEXT NOT NULL,
        meaning TEXT,
        created_at TEXT,
        last_review TEXT,
        interval INTEGER DEFAULT 0,
        repetition INTEGER DEFAULT 0,
        ef REAL DEFAULT 2.5,
        due_date TEXT
    )
    ''')
    conn.commit()

    # --- 兼容老库：补列（你之前已有 jp_kanji/jp_kana/jp_ruby 的补列逻辑，这里保留并幂等） ---
    cols = [c[1] for c in cur.execute('PRAGMA table_info(cards)').fetchall()]
    # 注意：language/unit/created_at/... 已在 CREATE TABLE 中，如老库缺失就补列
    if 'language' not in cols:
        cur.execute("ALTER TABLE cards ADD COLUMN language TEXT DEFAULT '日语'")
    if 'unit' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN unit TEXT DEFAULT ""')
    if 'meaning' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN meaning TEXT')
    if 'created_at' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN created_at TEXT')
    if 'last_review' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN last_review TEXT')
    if 'interval' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN interval INTEGER DEFAULT 0')
    if 'repetition' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN repetition INTEGER DEFAULT 0')
    if 'ef' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN ef REAL DEFAULT 2.5')
    if 'due_date' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN due_date TEXT')

    # 你原来追加的新列：jp_kanji / jp_kana / jp_ruby
    cols = [c[1] for c in cur.execute('PRAGMA table_info(cards)').fetchall()]
    if 'jp_kanji' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN jp_kanji TEXT')
    if 'jp_kana' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN jp_kana TEXT')
    if 'jp_ruby' not in cols:
        cur.execute('ALTER TABLE cards ADD COLUMN jp_ruby TEXT')
    conn.commit()

    # --- meta 版本表：无则建，空则置 0 ---
    cur.execute('CREATE TABLE IF NOT EXISTS meta (schema_version INTEGER NOT NULL)')
    row = cur.execute('SELECT schema_version FROM meta').fetchone()
    if row is None:
        cur.execute('INSERT INTO meta(schema_version) VALUES (0)')
        conn.commit()
        ver = 0
    else:
        ver = int(row[0] or 0)

    # --- 需要迁移：v0 -> v1 ---
    if ver < 1:
        # 迁移前做一次自动备份（非首次且文件存在才备份）
        if not first and os.path.exists(path):
            backup_db_file(path)

        try:
            cur.execute('BEGIN')

            # 1) 新表：reviews（记录每次学习/复习事件）
            cur.execute('''
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY,
                card_id INTEGER NOT NULL,
                ts TEXT NOT NULL,               -- ISO8601 时间戳
                mode INTEGER NOT NULL,          -- 0=日->中, 1=中->日
                quality INTEGER NOT NULL,       -- 1/3/4/5
                elapsed_ms INTEGER,             -- 可选：耗时
                rep_before INTEGER,
                rep_after INTEGER,
                ef_before REAL,
                ef_after REAL,
                int_before INTEGER,
                int_after INTEGER,
                due_before TEXT,
                due_after TEXT,
                FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE
            )
            ''')

            # 2) 索引：cards 与 reviews 的常用查询键（避免 IDE 误报 IF NOT EXISTS）
            ensure_index(cur, 'idx_cards_unit', 'CREATE INDEX idx_cards_unit ON cards(unit)')
            ensure_index(cur, 'idx_cards_last_review', 'CREATE INDEX idx_cards_last_review ON cards(last_review)')
            ensure_index(cur, 'idx_cards_due_date', 'CREATE INDEX idx_cards_due_date ON cards(due_date)')
            ensure_index(cur, 'idx_cards_term', 'CREATE INDEX idx_cards_term ON cards(term)')
            ensure_index(cur, 'idx_cards_kana', 'CREATE INDEX idx_cards_kana ON cards(jp_kana)')
            ensure_index(cur, 'idx_cards_kanji', 'CREATE INDEX idx_cards_kanji ON cards(jp_kanji)')

            ensure_index(cur, 'idx_reviews_card_ts', 'CREATE INDEX idx_reviews_card_ts ON reviews(card_id, ts)')
            ensure_index(cur, 'idx_reviews_ts', 'CREATE INDEX idx_reviews_ts ON reviews(ts)')

            # 3) 升级版本号
            cur.execute('UPDATE meta SET schema_version = 1')

            cur.execute('COMMIT')
        except Exception:
            cur.execute('ROLLBACK')
            raise

        # 可选：分析/压缩
        try:
            cur.execute('ANALYZE')
        except Exception:
            pass

    return conn


class SettingsManager:
    DEFAULTS = {
        # 复习/队列
        "include_not_due": False,  # 队列是否包含未到期
        "daily_limit": 100,  # 本次会话上限（0=不限）
        "shuffle_on_start": True,  # 打开复习窗口即打乱
        # 视图/UI
        "table_row_height": 28,  # 词表行高
        "op_col_width": 84,  # 操作列宽度
        "font_scale": 1.0,  # 全局字号缩放（1.0=不变）
        # 词典/补全
        "dict_path": "",  # 词典路径（留空用默认）
        "remember_zh_preference_persist": True,  # 记住“中文释义→候选”的偏好（持久化）
        "zh_prefer": {},  # {中文: (term, kana, meaning)}
        # 评分映射可保留默认 1/3/4/5，如需开放可加：hard/good/easy
        "unit_order": [],  # 左侧单元自定义顺序（不含“所有单元”）
    }

    def __init__(self, path: str):
        self.path = path
        self.data = dict(self.DEFAULTS)

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                    if isinstance(obj, dict):
                        self.data.update(obj)
        except Exception:
            pass
        return self

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, val):
        self.data[key] = val

    # —— 中文释义偏好（持久化）——
    def set_zh_prefer(self, zh: str, cand: tuple[str, str, str]):
        if not zh: return
        z = self.data.get("zh_prefer", {})
        z[zh] = list(cand)  # json 友好
        self.data["zh_prefer"] = z

    def get_zh_prefer(self, zh: str):
        z = self.data.get("zh_prefer", {})
        v = z.get(zh)
        return tuple(v) if isinstance(v, list) else v


CSV_HEADERS = [
    "unit", "kana", "kanji", "romaji", "meaning",
    "created_at", "last_review", "interval", "repetition", "ef", "due_date", "id"
]

_HEADER_ALIAS = {
    "单元": "unit", "unit": "unit",
    "假名": "kana", "词条": "kana", "term": "kana", "kana": "kana",
    "汉字": "kanji", "汉字写法": "kanji", "kanji": "kanji",
    "罗马音": "romaji", "romaji": "romaji", "roma": "romaji",
    "释义": "meaning", "中文": "meaning", "meaning": "meaning",
    "created_at": "created_at", "last_review": "last_review",
    "interval": "interval", "repetition": "repetition", "ef": "ef",
    "due": "due_date", "due_date": "due_date", "id": "id"
}


def _normalize_header(h: str) -> str:
    h = (h or "").strip().lower()
    return _HEADER_ALIAS.get(h, h)


def _detect_encoding(path: str) -> str:
    # 轻量多编码尝试：先 utf-8-sig，再 utf-8，再常见中文/日文编码
    for enc in ("utf-8-sig", "utf-8", "gb18030", "cp932", "shift_jis"):
        try:
            with open(path, "r", encoding=enc) as f:
                f.readline()
            return enc
        except Exception:
            continue
    return "utf-8"


def _read_csv_rows(path: str) -> list[dict]:
    enc = _detect_encoding(path)
    rows = []
    with open(path, "r", newline="", encoding=enc) as f:
        rd = csv.reader(f)
        headers = next(rd, None)
        if not headers:
            return []
        keys = [_normalize_header(h) for h in headers]
        for row in rd:
            item = {}
            for i, key in enumerate(keys):
                if not key or i >= len(row): continue
                item[key] = row[i].strip()
            rows.append(item)
    return rows


def _write_csv_rows(path: str, headers: list[str], rows: list[list[str]]):
    _ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(headers)
        wr.writerows(rows)


def insert_review(conn, card_id: int, mode: int, quality: int,
                  elapsed_ms: int | None = None,
                  before: dict | None = None, after: dict | None = None):
    from datetime import datetime
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    bf = before or {}
    af = after or {}
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO reviews (
        card_id, ts, mode, quality, elapsed_ms,
        rep_before, rep_after, ef_before, ef_after,
        int_before, int_after, due_before, due_after
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        card_id, now, mode, quality, elapsed_ms,
        bf.get("repetition"), af.get("repetition"),
        bf.get("ef"), af.get("ef"),
        bf.get("interval"), af.get("interval"),
        bf.get("due_date"), af.get("due_date"),
    ))
    conn.commit()


# --------------------------
# DB 操作
# --------------------------
def add_card(conn, language, unit, term, meaning, jp_kanji=None, jp_kana=None, jp_ruby=None):
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    due = None
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO cards (
        language, unit, term, meaning,
        created_at, last_review, interval, repetition, ef, due_date,
        jp_kanji, jp_kana, jp_ruby
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (language, unit, term, meaning,
          now, None, 0, 0, 2.5, due,
          jp_kanji, jp_kana, jp_ruby))
    conn.commit()
    return cur.lastrowid


def list_units(conn):
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT unit FROM cards ORDER BY unit')
    rows = [r[0] for r in cur.fetchall() if r[0] and r[0] != ""]
    return rows


# 1) DB 查询：支持单元/多单元/全部（统一不再按到期过滤）
def list_cards_by_unit(conn, unit=None):
    cur = conn.cursor()
    # 全部
    if unit is None or unit == "" or unit == "所有单元":
        cur.execute('SELECT * FROM cards ORDER BY unit, id')
        return cur.fetchall()
    # 多单元
    if isinstance(unit, (list, tuple, set)):
        units = []
        seen = set()
        for u in unit:
            name = (u or "").strip()
            if not name or name == "所有单元":
                continue
            if name in seen:
                continue
            seen.add(name)
            units.append(name)
        # 空列表直接回退到全部
        if not units:
            cur.execute('SELECT * FROM cards ORDER BY unit, id')
            return cur.fetchall()
        qs = ",".join("?" for _ in units)
        cur.execute(f'SELECT * FROM cards WHERE unit IN ({qs}) ORDER BY unit, id', units)
        return cur.fetchall()
    # 单一
    cur.execute('SELECT * FROM cards WHERE unit = ? ORDER BY id', (unit,))
    return cur.fetchall()


# 保留占位的兼容名，但实际不再按到期筛选
def list_due_cards(conn, unit=None):
    return list_cards_by_unit(conn, unit)


def update_card_review(conn, card_id, interval, repetition, ef, last_review, due_date):
    cur = conn.cursor()
    cur.execute('''
    UPDATE cards
    SET interval = ?, repetition = ?, ef = ?, last_review = ?, due_date = ?
    WHERE id = ?
    ''', (interval, repetition, ef, last_review, due_date, card_id))
    conn.commit()


def update_card_fields(conn, card_id, language, unit, term, meaning):
    cur = conn.cursor()
    cur.execute('''
    UPDATE cards
    SET language = ?, unit = ?, term = ?, meaning = ?
    WHERE id = ?
    ''', (language, unit, term, meaning, card_id))
    conn.commit()


def delete_card(conn, card_id):
    cur = conn.cursor()
    cur.execute('DELETE FROM cards WHERE id = ?', (card_id,))
    conn.commit()


def count_cards_in_unit(conn, unit: str) -> int:
    cur = conn.cursor()
    n = cur.execute('SELECT COUNT(*) FROM cards WHERE unit = ?', (unit,)).fetchone()[0]
    return int(n or 0)


def delete_unit(conn, unit: str):
    cur = conn.cursor()
    cur.execute('DELETE FROM cards WHERE unit = ?', (unit,))
    conn.commit()


# 1) 顶部 DB 操作区域附近（已有 import csv），新增/替换：完整导出函数
def export_all_cards_to_csv(conn, filepath: str):
    """
    导出 cards 全表为 CSV（含学习记录），UTF-8 BOM 防止 Excel 乱码。
    按当前表结构动态输出列，兼容老/新库（如 jp_kanji/jp_kana/jp_ruby 可选）。
    """
    cur = conn.cursor()
    cols_info = cur.execute('PRAGMA table_info(cards)').fetchall()
    if not cols_info:
        raise RuntimeError("数据库中不存在表 cards")

    # 按表定义顺序输出列
    col_names = [c[1] for c in cols_info]

    # 友好表头映射（无映射则保留原名）
    name_map = {
        "id": "ID",
        "language": "语言",
        "unit": "单元",
        "term": "假名",
        "meaning": "中文释义",
        "interval": "间隔",
        "repetition": "重复次数",
        "ef": "易度",
        "last_review": "上次复习时间",
        "due_date": "到期时间",
        "created_at": "创建时间",
        "jp_kanji": "汉字写法",
        "jp_kana": "假名读音",
        "jp_ruby": "读音标注",
    }
    header = [name_map.get(n, n) for n in col_names]

    rows = cur.execute(f'SELECT {", ".join(col_names)} FROM cards ORDER BY unit, id').fetchall()

    # 写 CSV：UTF-8 BOM，避免 Excel 乱码；newline='' 防止空行
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for r in rows:
            writer.writerow(["" if v is None else v for v in r])


class CardSortProxy(QtCore.QSortFilterProxyModel):
    """第0列显示 1..N 序号，同时支持多列快速过滤。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""
        self.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)

    def setQuery(self, q: str):
        self._query = (q or "").strip().lower()
        self.invalidateFilter()

    def data(self, index, role=QtCore.Qt.DisplayRole):
        # 让序号列始终显示为“代理行号 + 1”，
        # 即过滤/排序/打乱后的可见顺序 1..N
        if role == QtCore.Qt.DisplayRole and index.column() == 0:
            return str(index.row() + 1)
        return super().data(index, role)

    def filterAcceptsRow(self, source_row, source_parent):
        # 无查询：不过滤
        if not self._query:
            return True
        model = self.sourceModel()
        # 按列：1 假名、2 汉字、3 罗马音、4 释义
        cols = (1, 2, 3, 4)
        q = self._query
        for c in cols:
            idx = model.index(source_row, c, source_parent)
            val = model.data(idx, QtCore.Qt.DisplayRole)
            if val and q in str(val).lower():
                return True
        return False

    def lessThan(self, left: QtCore.QModelIndex, right: QtCore.QModelIndex) -> bool:
        """第0列（序号）按源模型的行号排序，保持 1..N 的‘可见顺序’；其它列按默认。"""
        if left.column() == 0 and right.column() == 0:
            return left.row() < right.row()
        return super().lessThan(left, right)


# --- M3: Model/View ---
class CardTableModel(QtCore.QAbstractTableModel):
    """
    列顺序与旧表头保持一致：
    0: ID (int)
    1: 假名 (r[3])
    2: 汉字写法 (r[11])
    3: 罗马音 (r[12])  # 你的库中 jp_kana 存放罗马音
    4: 释义 (r[4])
    5: 操作(虚拟列，由 Delegate 绘制按钮)
    """
    COLS = ["序号", "假名", "汉字写法", "罗马音", "释义", "操作"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []  # 每个元素是数据库整行 tuple
        # 排序用的角色：数值/时间可以放在 UserRole，显示给 DisplayRole
        self._sort_role = QtCore.Qt.UserRole + 1

    def rowCount(self, parent=QtCore.QModelIndex()):
        return len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role):
        if role == QtCore.Qt.DisplayRole and orientation == QtCore.Qt.Horizontal:
            return self.COLS[section]
        return None

    def data(self, index, role):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = index.column()

        def _s(row, i):
            return (row[i] or "").strip() if (
                    isinstance(row, (list, tuple)) and len(row) > i and row[i] is not None) else ""

        if role == self._sort_role:
            if col == 0:
                return index.row()
            elif col == 1:  # 假名(term)
                return _s(r, 3)
            elif col == 2:  # 汉字(jp_kanji)
                return _s(r, 11)
            elif col == 3:  # 罗马音(用 jp_kana 存放罗马音或留空)
                return _s(r, 12)
            elif col == 4:  # 释义
                return _s(r, 4)
            else:
                return ""

        if role == QtCore.Qt.DisplayRole:
            if col == 0:
                return str(index.row() + 1)
            elif col == 1:
                return _s(r, 3)
            elif col == 2:
                return _s(r, 11)
            elif col == 3:
                return _s(r, 12)
            elif col == 4:
                return _s(r, 4)
            elif col == 5:
                return ""
        return None

    def card_id_at(self, row_idx):
        r = self.row_at(row_idx)
        try:
            return int(r[0]) if (r and len(r) > 0 and r[0] is not None) else -1
        except Exception:
            return -1

    def flags(self, index):
        if not index.isValid():
            return QtCore.Qt.NoItemFlags
        # 全表只读，由对话框编辑
        return QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = list(rows) if rows else []
        self.endResetModel()

    def row_at(self, row_idx):
        if 0 <= row_idx < len(self._rows):
            return self._rows[row_idx]
        return None

    def card_id_at(self, row_idx):
        r = self.row_at(row_idx)
        return int(r[0]) if r and r[0] is not None else -1


class CandidatePopup(QtWidgets.QListWidget):
    """
    中文释义 -> 多候选下拉选择器。发射 candidate_chosen((term,kana,meaning), remember)
    Ctrl+回车/点击时按住 Ctrl 视为“记住此选择”（remember=True）
    """
    candidate_chosen = QtCore.pyqtSignal(tuple, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self._cur_zh = ""

    def show_for(self, anchor: QtWidgets.QWidget, zh: str, candidates: list[tuple[str, str, str]]):
        self.clear()
        self._cur_zh = zh
        for (term, kana, mean) in candidates:
            # kana 若是英文字母，不当成假名展示；优先展示片假名（term）
            def _looks_kana(s: str) -> bool:
                if not s: return False
                for ch in s:
                    code = ord(ch)
                    if ch.isspace():
                        continue
                    if (0x3040 <= code <= 0x309F) or (0x30A0 <= code <= 0x30FF) or \
                            (0x31F0 <= code <= 0x31FF) or (0xFF66 <= code <= 0xFF9D) or \
                            ch in ("ー", "・"):
                        continue
                    return False
                return True

            disp_kana = kana if _looks_kana(kana) else (term or kana)
            text = f"{disp_kana} 〔{term}〕 — {mean}"
            it = QtWidgets.QListWidgetItem(text)
            it.setData(QtCore.Qt.UserRole, (term, kana, mean))
            self.addItem(it)

        if not candidates:
            self.hide()
            return
        self.setCurrentRow(0)

        # 定位到输入框正下方
        p = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height()))
        w = max(360, anchor.width())
        self.setGeometry(p.x(), p.y(), w, min(320, self.sizeHintForRow(0) * min(8, len(candidates)) + 8))
        self.show()
        self.raise_()
        # 不抢焦点，允许用户在“中文释义”里按 Enter 直接结束输入
        # self.setFocus()
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        if e.key() == QtCore.Qt.Key_Escape:
            self.hide();
            return
        if e.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            it = self.currentItem()
            if it:
                tup = it.data(QtCore.Qt.UserRole)
                remember = bool(e.modifiers() & QtCore.Qt.ControlModifier)
                self.candidate_chosen.emit(tup, remember)
            self.hide();
            return
        super().keyPressEvent(e)

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent):
        it = self.itemAt(e.pos())
        if it:
            tup = it.data(QtCore.Qt.UserRole)
            remember = bool(e.modifiers() & QtCore.Qt.ControlModifier)
            self.candidate_chosen.emit(tup, remember)
            self.hide()
        else:
            super().mouseReleaseEvent(e)

    def focusOutEvent(self, e: QtGui.QFocusEvent):
        self.hide()
        super().focusOutEvent(e)

class KanaCandidatePopup(QtWidgets.QListWidget):
    kana_chosen = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

    def show_for(self, anchor: QtWidgets.QWidget, candidates: list[str]):
        self.clear()
        for s in candidates:
            if not s:
                continue
            it = QtWidgets.QListWidgetItem(s)
            it.setData(QtCore.Qt.UserRole, s)
            self.addItem(it)
        if self.count() == 0:
            self.hide();
            return
        self.setCurrentRow(0)
        p = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height()))
        w = max(260, anchor.width())
        h = min(120, self.sizeHintForRow(0) * self.count() + 8)
        self.setGeometry(p.x(), p.y(), w, h)
        self.show(); self.raise_()
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        if e.key() == QtCore.Qt.Key_Escape:
            self.hide(); return
        if e.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            it = self.currentItem()
            if it:
                self.kana_chosen.emit(it.data(QtCore.Qt.UserRole))
            self.hide();
            return
        super().keyPressEvent(e)

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent):
        it = self.itemAt(e.pos())
        if it:
            self.kana_chosen.emit(it.data(QtCore.Qt.UserRole))
            self.hide()
        else:
            super().mouseReleaseEvent(e)

    def focusOutEvent(self, e: QtGui.QFocusEvent):
        self.hide()
        super().focusOutEvent(e)



class OpButtonDelegate(QtWidgets.QStyledItemDelegate):
    """
    操作列 Delegate：绘制“编辑 / 删除”胶囊按钮，处理点击。
    对外发射两个信号，传 card_id。
    """
    editRequested = QtCore.pyqtSignal(int)
    deleteRequested = QtCore.pyqtSignal(int)

    def __init__(self, model: CardTableModel, parent=None):
        super().__init__(parent)
        self.model = model
        self._padding = 6
        self._btn_w = 56
        self._btn_h = 24
        self._gap = 8

    def paint(self, painter, option, index):
        painter.save()
        r = option.rect

        # 两个按钮的矩形
        edit_rect = QtCore.QRect(
            r.left() + self._padding,
            r.center().y() - self._btn_h // 2,
            self._btn_w, self._btn_h
        )
        del_rect = QtCore.QRect(
            edit_rect.right() + self._gap,
            edit_rect.top(),
            self._btn_w, self._btn_h
        )

        # 绘制按钮外观（跟你现有样式尽量接近）
        def draw_btn(rect, text, danger=False):
            path = QtGui.QPainterPath()
            path.addRoundedRect(rect, 8, 8)
            if danger:
                painter.fillPath(path, QtGui.QColor("#ffebee"))
                pen = QtGui.QPen(QtGui.QColor("#c62828"))
            else:
                painter.fillPath(path, QtGui.QColor("#e3f2fd"))
                pen = QtGui.QPen(QtGui.QColor("#1565c0"))
            painter.setPen(pen)
            painter.drawPath(path)
            painter.drawText(rect, QtCore.Qt.AlignCenter, text)

        draw_btn(edit_rect, "编辑", danger=False)
        draw_btn(del_rect, "删除", danger=True)

        painter.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.LeftButton:
            r = option.rect
            edit_rect = QtCore.QRect(r.left() + self._padding, r.center().y() - self._btn_h // 2, self._btn_w,
                                     self._btn_h)
            del_rect = QtCore.QRect(edit_rect.right() + self._gap, edit_rect.top(), self._btn_w, self._btn_h)
            pos = event.pos()

            # 关键：把代理索引映射回源模型
            try:
                if isinstance(model, QtCore.QSortFilterProxyModel):
                    src_index = model.mapToSource(index)
                    row = src_index.row()
                else:
                    row = index.row()
                cid = self.model.card_id_at(row)
            except Exception:
                return False

            if edit_rect.contains(pos):
                self.editRequested.emit(cid);
                return True
            if del_rect.contains(pos):
                self.deleteRequested.emit(cid);
                return True
        return False

class LocalJaZhDict:
    def __init__(self, path: str | None = None):
        self.path = (path or "").strip()
        self._loaded = False
        self._map: dict[str, str] = {}  # key -> meaning
        self._entry: dict[str, tuple[str, str, str]] = {}  # key -> (term, kana, meaning)

    @staticmethod
    def _norm_key(s: str) -> str:
        if not s:
            return ""
        s = s.replace("\r", "").replace("\n", " ").strip()
        s = s.replace("\u3000", " ")
        s = s.replace("～", "~")
        s = s.replace("［", "").replace("］", "")
        s = s.replace("（", "(").replace("）", ")")
        while "  " in s:
            s = s.replace("  ", " ")
        return s

    @staticmethod
    def _clean_path(p: str) -> str:
        p = (p or "").strip().strip("'\"")
        if not p:
            return ""
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.abspath(p)
        return p

    @staticmethod
    def _is_kana(s: str) -> bool:
        if not s:
            return False
        for ch in s:
            code = ord(ch)
            if ch.isspace():
                continue
            if (
                    0x3040 <= code <= 0x309F or  # ひらがな
                    0x30A0 <= code <= 0x30FF or  # カタカナ
                    0x31F0 <= code <= 0x31FF or  # 小假名
                    0xFF66 <= code <= 0xFF9D or  # 半角片假名
                    ch in ("ー", "・")
            ):
                continue
            return False
        return True

    def load(self, path: str | None = None):
        if self._loaded and not path:
            return
        p = self._clean_path(path or self.path)
        self.path = p
        self._loaded = True
        self._map = {}
        self._entry = {}
        if not p or not os.path.exists(p):
            print(f"[LocalJaZhDict] 未找到词典文件: {p}")
            return

        def _store_entry(m_mean: dict[str, str], m_entry: dict[str, tuple[str, str, str]], term: str, kana: str,
                         meaning: str):
            # 建立多键索引：原样与规范化；包含 term 与 kana（若合法）
            keys = []
            if term:
                keys += [term, self._norm_key(term)]
            if kana and self._is_kana(kana):
                keys += [kana, self._norm_key(kana)]
            # 去空去重
            keys = [k for k in dict.fromkeys([k for k in keys if k])]
            for k in keys:
                m_mean[k] = meaning
                m_entry[k] = (term, kana, meaning)

        def _read_csv(encoding: str) -> tuple[dict[str, str], dict[str, tuple[str, str, str]]]:
            m_mean: dict[str, str] = {}
            m_entry: dict[str, tuple[str, str, str]] = {}
            with open(p, "r", encoding=encoding, newline="") as f:
                rd = csv.reader(f)
                for i, row in enumerate(rd):
                    if not row:
                        continue
                    # 跳过表头
                    if i == 0 and row[0].strip().lower() in ("term", "词条"):
                        continue
                    # 兼容 2/3 列：term[,kana],meaning
                    if len(row) < 2:
                        continue
                    term = (row[0] or "").strip()
                    kana = (row[1] or "").strip() if len(row) >= 3 else ""
                    meaning = (row[2] if len(row) >= 3 else row[1] or "").strip()
                    if not term or not meaning:
                        continue
                    _store_entry(m_mean, m_entry, term, kana, meaning)
            return m_mean, m_entry

        try:
            m1, e1 = _read_csv("utf-8-sig")
            self._map, self._entry = m1, e1
            print(f"[LocalJaZhDict] 已加载 {len(self._entry)} 键，文件: {p}")
        except UnicodeDecodeError:
            try:
                m2, e2 = _read_csv("utf-8")
                self._map, self._entry = m2, e2
                print(f"[LocalJaZhDict] 已加载 {len(self._entry)} 键（utf-8），文件: {p}")
            except Exception as e:
                print(f"[LocalJaZhDict] 加载失败: {e}")
                self._map, self._entry = {}, {}
        except Exception as e:
            print(f"[LocalJaZhDict] 加载失败: {e}")
            self._map, self._entry = {}, {}

    def get(self, term: str) -> str:
        if not self._loaded:
            self.load()
        t = (term or "").strip()
        if not t:
            return ""
        if t in self._map:
            return self._map[t]
        t2 = self._norm_key(t)
        return self._map.get(t2, "")

    def get_full(self, key: str) -> tuple[str, str, str] | None:
        if not self._loaded:
            self.load()
        k = (key or "").strip()
        if not k:
            return None
        if k in self._entry:
            return self._entry[k]
        k2 = self._norm_key(k)
        return self._entry.get(k2, None)

    def search_by_meaning(self, zh: str, limit: int = 8) -> list[tuple[str, str, str]]:
        """
        通过中文释义反查，返回若干 (term, kana, meaning) 候选，按相关度排序：
          完全匹配 > 前缀匹配 > 子串匹配；有假名优先；term 含汉字优先。
        """
        if not getattr(self, "_loaded", False):
            self.load()
        q = (zh or "").strip()
        if not q:
            return []
        q_low = q.lower()
        seen = set()
        cand = []

        for _k, tup in self._entry.items():
            term, kana, mean = tup
            if not mean:
                continue
            m_low = mean.lower()

            score = 0
            if m_low == q_low:
                score += 100
            elif m_low.startswith(q_low):
                score += 60
            elif q_low in m_low:
                score += 40
            else:
                continue

            if kana:
                score += 3
            if term and any(0x4E00 <= ord(c) <= 0x9FFF for c in term):
                score += 2

            key = (term or "", kana or "", mean or "")
            if key in seen:
                continue
            seen.add(key)
            cand.append((score, term or "", kana or "", mean or ""))

        cand.sort(key=lambda x: (-x[0], -len(x[2]), -len(x[1])))
        return [(t, k, m) for _, t, k, m in cand[:limit]]

    def search_full_by_meaning_best(self, zh: str) -> tuple[str, str, str] | None:
        res = self.search_by_meaning(zh, limit=1)
        return res[0] if res else None


# --- 2) 模块级：初始化与帮助函数（保持已有 suggest_zh_meaning，仅新增 suggest_full_entry） ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCAL_CSV = os.path.join(BASE_DIR, "tools", "vocab_local_jazh.csv")

_LOCAL_DICT = LocalJaZhDict(os.environ.get("LOCAL_JAZH_CSV") or DEFAULT_LOCAL_CSV)
_LOCAL_DICT.load()


def _get_local_meaning(term: str) -> str:
    return _LOCAL_DICT.get(term)


def suggest_zh_meaning(term: str) -> str:
    term = (term or "").strip()
    if not term:
        return ""
    return _get_local_meaning(term) or ""

def suggest_full_entry(key: str) -> tuple[str, str, str] | None:
    # 返回 (term, kana, meaning) 或 None
    return _LOCAL_DICT.get_full(key or "")


def suggest_from_zh_meaning(zh: str) -> tuple[str, str, str] | None:
    try:
        return _LOCAL_DICT.search_full_by_meaning_best(zh or "")
    except Exception:
        return None

# --------------------------
# SM-2 算法（保持不变）
# --------------------------
def sm2_update(card_row, quality):
    interval = card_row[7]
    repetition = card_row[8]
    ef = card_row[9]
    q = quality
    if q < 3:
        repetition = 0
        interval = 1
    else:
        repetition += 1
        if repetition == 1:
            interval = 1
        elif repetition == 2:
            interval = 6
        else:
            interval = max(1, int(round(interval * ef)))
    ef = ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    if ef < 1.3:
        ef = 1.3
    return interval, repetition, ef


# 4) 帮助函数：拼显示 & 答案集合 ---------------------------------------
def format_jp_term(row):
    # 旧库：行长 11；新库：>=14
    jp_kanji = row[11] if len(row) > 11 else None
    jp_kana = row[12] if len(row) > 12 else None
    if (jp_kanji and jp_kanji.strip()) and (jp_kana and jp_kana.strip()):
        return f"{jp_kanji}｜{jp_kana}"
    if jp_kanji and jp_kanji.strip():
        return jp_kanji
    if jp_kana and jp_kana.strip():
        return jp_kana
    return row[3]  # 回退到 term


def jp_answer_set(row):
    s = set()
    s.add((row[3] or "").strip())
    if len(row) > 11:
        if row[11]: s.add(row[11].strip())
        if row[12]: s.add(row[12].strip())
    # 去空
    return {x for x in s if x}


def norm(s):
    return (s or "").strip()


# 1) 新增两个辅助函数（放在已有的 format_jp_term/jp_answer_set/norm 之后）
def format_jp_term_for_table(row):
    # 索引：id, language, unit, term, meaning, ..., jp_kanji(11), jp_kana(12)
    term = (row[3] or "").strip()
    jp_kanji = row[11].strip() if len(row) > 11 and row[11] else ""
    return f"{term} | {jp_kanji}" if term and jp_kanji else term or jp_kanji


# 保持“假名读音”列为 jp_kana
def get_jp_kanji(row):
    return (row[11] or "").strip() if len(row) > 11 and row[11] else ""


def get_jp_kana(row):
    return (row[12] or "").strip() if len(row) > 12 and row[12] else ""


# --- 填充工具函数（占位补全） ---
def _find_duplicate_card(conn, unit: str, kana: str, kanji: str) -> int | None:
    """
    简单重复策略：同 unit 且 (假名相同 或 汉字相同) 视为重复，返回 id
    """
    unit = (unit or "").strip()
    kana = (kana or "").strip()
    kanji = (kanji or "").strip()
    cur = conn.cursor()
    if kana and kanji:
        row = cur.execute(
            "SELECT id FROM cards WHERE unit=? AND (term=? OR jp_kanji=?) LIMIT 1",
            (unit, kana, kanji)
        ).fetchone()
    elif kana:
        row = cur.execute(
            "SELECT id FROM cards WHERE unit=? AND term=? LIMIT 1",
            (unit, kana)
        ).fetchone()
    elif kanji:
        row = cur.execute(
            "SELECT id FROM cards WHERE unit=? AND jp_kanji=? LIMIT 1",
            (unit, kanji)
        ).fetchone()
    else:
        row = None
    return int(row[0]) if row else None


def _has_kana(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        code = ord(ch)
        if (0x3040 <= code <= 0x309F) or (0x30A0 <= code <= 0x30FF):
            return True
    return False


def _has_kanji(s: str) -> bool:
    if not s:
        return False
    for ch in s:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF:
            return True
    return False


def _kata_to_hira(s: str) -> str:
    out = []
    for ch in s or "":
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))  # カタカナ→ひらがな
        else:
            out.append(ch)
    return "".join(out)

def _hira_to_kata(s: str) -> str:
    """平→片（基础范围）；保留 ー、・ 等符号"""
    res = []
    for ch in s:
        code = ord(ch)
        if 0x3041 <= code <= 0x3096:  # ぁ..ゖ
            res.append(chr(code + 0x60))
        elif ch == "ゔ":
            res.append("ヴ")
        elif ch == "ゕ":
            res.append("ヵ")
        elif ch == "ゖ":
            res.append("ヶ")
        else:
            res.append(ch)
    return "".join(res)

def romaji_to_kana_relaxed(s: str) -> tuple[str, str]:
    """
    宽松版：给“罗马音输入法”用。
    - 先尝试严格解析；
    - 若失败，退回到【最长合法前缀】；
    - 永远返回 (hira, kata) 二元组，不抛异常、不返回 None。
    """
    s = (s or "").strip().lower()
    if not s:
        return ("", "")
    res = None
    try:
        res = romaji_to_kana(s)   # 你现有的严格版
    except Exception:
        res = None
    if res:
        return res

    # 逐步裁掉尾部未成形前缀（如单独的 s / sh / k 等）
    for j in range(len(s) - 1, -1, -1):
        try:
            r = romaji_to_kana(s[:j])
        except Exception:
            r = None
        if r:
            return r

    # 全部无效就返回空候选
    return ("", "")



def kana_to_romaji(kana: str) -> str:
    if not kana:
        return ""
    k = _kata_to_hira(kana)

    digraph = {
        "きゃ": "kya", "きゅ": "kyu", "きょ": "kyo",
        "ぎゃ": "gya", "ぎゅ": "gyu", "ぎょ": "gyo",
        "しゃ": "sha", "しゅ": "shu", "しょ": "sho",
        "じゃ": "ja", "じゅ": "ju", "じょ": "jo",
        "ちゃ": "cha", "ちゅ": "chu", "ちょ": "cho",
        "にゃ": "nya", "にゅ": "nyu", "にょ": "nyo",
        "ひゃ": "hya", "ひゅ": "hyu", "ひょ": "hyo",
        "びゃ": "bya", "びゅ": "byu", "びょ": "byo",
        "ぴゃ": "pya", "ぴゅ": "pyu", "ぴょ": "pyo",
        "みゃ": "mya", "みゅ": "myu", "みょ": "myo",
        "りゃ": "rya", "りゅ": "ryu", "りょ": "ryo",
        "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo",
    }
    mono = {
        "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
        "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
        "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
        "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
        "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
        "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
        "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
        "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
        "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
        "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
        "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
        "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
        "や": "ya", "ゆ": "yu", "よ": "yo",
        "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
        "わ": "wa", "を": "o", "ん": "n",
        "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
        "ゔ": "vu", "ゎ": "wa", "ゕ": "ka", "ゖ": "ka",
        "ー": "-",  # 长音符，后面单独处理
        "っ": "",  # 促音，靠后面首辅音加倍
    }

    res = []
    i = 0
    sokuon = False
    while i < len(k):
        ch = k[i]
        if ch == "っ":
            sokuon = True
            i += 1
            continue

        token = None
        if i + 1 < len(k):
            pair = k[i:i + 2]
            if pair in digraph:
                token = digraph[pair]
                i += 2
        if token is None:
            token = mono.get(ch, ch)
            i += 1

        if sokuon and token and token[0] in "bcdfghjklmnpqrstvwxyz":
            token = token[0] + token
        sokuon = False
        res.append(token)

    out = []
    last_vowel = ""
    vowels = "aeiou"
    for token in res:
        if token == "-":
            # 用上一个 token 的元音延长
            out.append(last_vowel if last_vowel else "")
            continue
        out.append(token)
        for c in reversed(token):
            if c in vowels:
                last_vowel = c
                break
    return "".join(out)


def romaji_to_kana(roma: str) -> tuple[str, str] | None:
    """
    把罗马音解析成（平假名, 片假名）。
    规则支持：
    - ん 用单个 'n'；在词末或辅音前解析为「ん」；
    - つ 用 'tsu'；
    - 小假名用 'x' 前缀：'xa','xi','xu','xe','xo','xya','xyu','xyo','xtsu'；
    - 拗音既支持 'kyo'，也支持 'ki' + 'xyo'（即 'kixyo'）。
    - 仅做“宽松可用”的最小实现，不强求全量罗马音规范。
    """
    if not roma:
        return None
    s = roma.strip().lower()

    # 小假名 & 特例（x 前缀）
    xmap = {
        "xa": "ぁ", "xi": "ぃ", "xu": "ぅ", "xe": "ぇ", "xo": "ぉ",
        "xya": "ゃ", "xyu": "ゅ", "xyo": "ょ",
        "xtsu": "っ", "xwa": "ゎ"
    }

    # 拗音（二合）
    digraph = {
        "kya": "きゃ", "kyu": "きゅ", "kyo": "きょ",
        "gya": "ぎゃ", "gyu": "ぎゅ", "gyo": "ぎょ",
        "sha": "しゃ", "shu": "しゅ", "sho": "しょ",
        "ja": "じゃ",  "ju": "じゅ",  "jo": "じょ",
        "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ",
        "nya": "にゃ", "nyu": "にゅ", "nyo": "にょ",
        "hya": "ひゃ", "hyu": "ひゅ", "hyo": "ひょ",
        "bya": "びゃ", "byu": "びゅ", "byo": "びょ",
        "pya": "ぴゃ", "pyu": "ぴゅ", "pyo": "ぴょ",
        "mya": "みゃ", "myu": "みゅ", "myo": "みょ",
        "rya": "りゃ", "ryu": "りゅ", "ryo": "りょ",
    }

    # 单音（基本五十音）
    mono = {
        "a":"あ","i":"い","u":"う","e":"え","o":"お",
        "ka":"か","ki":"き","ku":"く","ke":"け","ko":"こ",
        "ga":"が","gi":"ぎ","gu":"ぐ","ge":"げ","go":"ご",
        "sa":"さ","shi":"し","su":"す","se":"せ","so":"そ",
        "za":"ざ","ji":"じ","zu":"ず","ze":"ぜ","zo":"ぞ",
        "ta":"た","chi":"ち","tsu":"つ","te":"て","to":"と",
        "da":"だ","di":"ぢ","du":"づ","de":"で","do":"ど",
        "na":"な","ni":"に","nu":"ぬ","ne":"ね","no":"の",
        "ha":"は","hi":"ひ","fu":"ふ","he":"へ","ho":"ほ",
        "ba":"ば","bi":"び","bu":"ぶ","be":"べ","bo":"ぼ",
        "pa":"ぱ","pi":"ぴ","pu":"ぷ","pe":"ぺ","po":"ぽ",
        "ma":"ま","mi":"み","mu":"む","me":"め","mo":"も",
        "ya":"や","yu":"ゆ","yo":"よ",
        "ra":"ら","ri":"り","ru":"る","re":"れ","ro":"ろ",
        "wa":"わ","wo":"を",
    }

    # 允许 "ki"+"xyo" 这种分拆：先把 kixyo / gixyo … 归并成 digraph
    # 做法：先替换所有 {辅音+i}+xya/xyo/xyu → 对应拗音
    def _merge_split_x(d: str) -> str:
        reps = [
            ("kixya", "kya"), ("kixyu", "kyu"), ("kixyo", "kyo"),
            ("gixya", "gya"), ("gixyu", "gyu"), ("gixyo", "gyo"),
            ("sixya", "sha"), ("sixyu", "shu"), ("sixyo", "sho"),
            ("shixya","sha"), ("shixyu","shu"), ("shixyo","sho"),
            ("jixya", "ja"),  ("jixyu", "ju"),  ("jixyo", "jo"),
            ("chixya","cha"), ("chixyu","chu"), ("chixyo","cho"),
            ("nixya", "nya"), ("nixyu", "nyu"), ("nixyo", "nyo"),
            ("hixya", "hya"), ("hixyu", "hyu"), ("hixyo", "hyo"),
            ("bixya", "bya"), ("bixyu", "byu"), ("bixyo", "byo"),
            ("pixya", "pya"), ("pixyu", "pyu"), ("pixyo", "pyo"),
            ("mixya", "mya"), ("mixyu", "myu"), ("mixyo", "myo"),
            ("rixya", "rya"), ("rixyu", "ryu"), ("rixyo", "ryo"),
        ]
        for a, b in reps:
            d = d.replace(a, b)
        return d

    s = _merge_split_x(s)
    i, out = 0, []

    def _peek(n: int) -> str:
        return s[i:i+n]

    vowels = set("aeiou")

    while i < len(s):
        # 1) 小假名（x 开头）
        if _peek(1) == "x":
            # 尽量匹配长的
            if _peek(4) in xmap:
                out.append(xmap[_peek(4)]); i += 4; continue
            if _peek(3) in xmap:
                out.append(xmap[_peek(3)]); i += 3; continue
            if _peek(2) in xmap:
                out.append(xmap[_peek(2)]); i += 2; continue
            # 无法解析就按字母丢弃或直接返回失败
            return None

        # 2) 拗音
        if _peek(3) in digraph:
            out.append(digraph[_peek(3)]); i += 3; continue

        # 3) 单音，优先三字母（shi/chi/tsu）
        if _peek(3) in mono:
            out.append(mono[_peek(3)]); i += 3; continue
        # 两字母（ka,ki,...）
        if _peek(2) in mono:
            out.append(mono[_peek(2)]); i += 2; continue
        # 单元音
        if _peek(1) in mono:
            out.append(mono[_peek(1)]); i += 1; continue

        # 4) ん：单个 'n' 在词末或非元音（且非 y）前解析为「ん」
        if _peek(1) == "n":
            nxt = s[i+1:i+2]
            if (not nxt) or (nxt and (nxt not in vowels and nxt != "y")):
                out.append("ん"); i += 1; continue
            # 若后面是元音或 y，则把 n 交给后续音节（如 na/nya）
            # 这里不前进，让上面的规则去匹配
        # 5) 其他（不识别的情况）
        return None

    hira = "".join(out)
    kata = _hira_to_kata(hira)
    return (hira, kata)

def _is_romaji(s: str) -> bool:
    if not s:
        return False
    if _has_kana(s) or _has_kanji(s):
        return False
    try:
        s.encode("ascii")
        return True
    except Exception:
        return False


def pick_kana(row) -> str:
    jp_kana = get_jp_kana(row)
    if _has_kana(jp_kana):
        return jp_kana.strip()
    term = norm(row[3])
    return term if _has_kana(term) else ""


def pick_romaji(row, kana_text: str) -> str:
    jp_kana = get_jp_kana(row)
    if _is_romaji(jp_kana):
        return jp_kana.strip()
    return kana_to_romaji(kana_text) if kana_text else ""


# --- 2) 新增：按 id 读取完整卡片 ---
def get_card_by_id(conn, card_id):
    cur = conn.cursor()
    cur.execute('SELECT * FROM cards WHERE id = ?', (card_id,))
    return cur.fetchone()


# --- 3) 新增：完整字段更新（不影响你原来的 update_card_fields） ---
def update_card_fields_full(conn, card_id, language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby=None):
    cur = conn.cursor()
    cur.execute('''
        UPDATE cards
        SET language = ?, unit = ?, term = ?, meaning = ?,
            jp_kanji = ?, jp_kana = ?, jp_ruby = ?
        WHERE id = ?
    ''', (language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby, card_id))
    conn.commit()


# 5.5 parse_iso：补全占位
def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None


# ==========================================
# 2. 新的字体加载与样式生成逻辑 (替换原有的 APP_STYLE 和 _install_app_fonts)
# ==========================================

def load_custom_font(app):
    """
    加载本地字体，返回准确的 family name。
    如果加载失败，返回系统默认的 fallback 列表。
    """
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # 优先寻找 Zen Maru Gothic (圆体，适合日文阅读)
    font_path = os.path.join(base_dir, "fonts", "ZenMaruGothic-Medium.ttf")
    if not os.path.exists(font_path):
        # 尝试根目录
        font_path = os.path.join(base_dir, "ZenMaruGothic-Medium.ttf")

    family_name = "Microsoft YaHei" # 默认兜底

    if os.path.exists(font_path):
        font_id = QtGui.QFontDatabase.addApplicationFont(font_path)
        if font_id != -1:
            loaded_families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
            if loaded_families:
                family_name = loaded_families[0]
                print(f"Font loaded: {family_name}")

    # 设置应用程序级别的默认字体
    default_font = QtGui.QFont(family_name, 11) # 默认 11pt
    default_font.setStyleStrategy(QtGui.QFont.PreferAntialias)
    app.setFont(default_font)

    return family_name

def get_app_style(font_family):
    """
    动态生成 CSS。
    关键修复：
    1. 移除所有 font-weight: bold/600，因为 Zen Maru Gothic Medium 本身就够粗了。
    2. 增加按钮高度和 Padding，防止文字显示不全。
    """
    # 构造字体栈
    font_stack = f'"{font_family}", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "sans-serif"'

    return f"""
    /* === 全局重置 === */
    QWidget {{
        font-family: {font_stack};
        font-size: 14px;       /* 稍微调小一点，Medium 字体显大 */
        font-weight: normal;   /* 关键！禁止伪粗体渲染 */
        color: #374151;
        outline: none;
    }}
    
    QMainWindow {{ background: #f9fafb; }}

    /* === 标题 (可以保留 bold，因为标题字号大，伪粗体影响较小，或者也可以去掉) === */
    QLabel#appTitle {{
        font-size: 22px; 
        font-weight: normal; /* 改为 normal，依靠字体本身的粗度 */
        color: #111827;
        padding: 8px 0;
    }}
    
    QLabel#sectionTitle {{
        font-size: 16px; 
        font-weight: normal; 
        color: #111827; 
        margin-top: 12px;
        margin-bottom: 6px;
    }}
    
    QLabel#muted {{ color: #6b7280; font-size: 13px; }}
    
    /* === 面板 === */
    QFrame#panel {{
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 12px;
    }}

    /* === 输入框 === */
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
        background: #ffffff;
        border: 1px solid #d1d5db;
        border-radius: 8px;
        padding: 8px 10px;  /* 增加垂直 padding */
        selection-background-color: #3b82f6;
        min-height: 20px;   /* 确保有足够高度显示文字 */
    }}
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
        border: 1px solid #3b82f6;
        background: #ffffff;
    }}
    QComboBox::drop-down {{ border: 0px; width: 24px; }}

    /* === 列表与表格 === */
    QListWidget, QTableWidget {{
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        alternate-background-color: #f9fafb; /* 日间模式交替行颜色 */
        gridline-color: #e5e7eb;
    }}
    QListWidget::item, QTableWidget::item {{
        padding: 6px;
        border-bottom: 0px; 
    }}
    QListWidget::item:selected, QTableWidget::item:selected {{
        background: #eff6ff; 
        color: #1d4ed8; 
    }}
    QHeaderView::section {{
        background-color: #f3f4f6;
        padding: 8px;
        border: none;
        border-bottom: 1px solid #e5e7eb;
        font-weight: normal; /* 表头也不要加粗 */
        color: #4b5563;
    }}

    /* === 按钮系统 === */
    QPushButton {{
        border-radius: 8px;
        padding: 8px 16px;     /* 增加 Padding */
        font-weight: normal;   /* 关键：修复字体显示不全/糊字的问题 */
        border: 1px solid transparent;
        min-height: 20px;      /* 确保高度 */
    }}
    
    /* 主要按钮 (蓝色) */
    QPushButton#primary {{
        background-color: #3b82f6;
        color: white;
    }}
    QPushButton#primary:hover {{ background-color: #2563eb; }}
    QPushButton#primary:pressed {{ background-color: #1d4ed8; }}
    QPushButton#primary:disabled {{ background-color: #93c5fd; color: #eff6ff; }}

    /* 次要按钮 (白色) */
    QPushButton#secondary {{
        background-color: #ffffff;
        color: #374151;
        border: 1px solid #d1d5db;
    }}
    QPushButton#secondary:hover {{ background-color: #f9fafb; border-color: #9ca3af; }}
    
    /* 危险按钮 */
    QPushButton#miniDanger {{
        background-color: #fee2e2; 
        color: #b91c1c;
        padding: 4px 8px;
    }}
    QPushButton#miniDanger:hover {{ background-color: #fecaca; }}
    
    /* 迷你按钮 */
    QPushButton#mini {{
        background-color: #f3f4f6;
        color: #374151;
        padding: 4px 8px;
    }}
    QPushButton#mini:hover {{ background-color: #e5e7eb; }}

    /* 复习按钮颜色保持不变 */
    QPushButton#again {{ background-color: #fee2e2; color: #b91c1c; border: 1px solid #fecaca; }}
    QPushButton#again:hover {{ background-color: #fecaca; }}
    QPushButton#hard {{ background-color: #fef3c7; color: #b45309; border: 1px solid #fde68a; }}
    QPushButton#hard:hover {{ background-color: #fde68a; }}
    QPushButton#good {{ background-color: #d1fae5; color: #047857; border: 1px solid #a7f3d0; }}
    QPushButton#good:hover {{ background-color: #a7f3d0; }}
    QPushButton#easy {{ background-color: #dbeafe; color: #1e40af; border: 1px solid #bfdbfe; }}
    QPushButton#easy:hover {{ background-color: #bfdbfe; }}

    /* 学习大字 */
    QLabel#bigterm {{
        font-size: 48px;
        font-weight: normal;
        color: #111827;
        qproperty-alignment: AlignCenter;
        padding: 20px 0;
    }}
    """

def _register_zen_maru_font():
    """注册 Zen Maru Gothic 字体，并把它设为 QApplication 的默认字体。"""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base, "ZenMaruGothic-Medium.ttf"),
            os.path.join(base, "fonts", "ZenMaruGothic-Medium.ttf"),
        ]
        for p in candidates:
            if os.path.exists(p):
                fid = QtGui.QFontDatabase.addApplicationFont(p)
                if fid != -1:
                    fams = QtGui.QFontDatabase.applicationFontFamilies(fid)
                    if fams:
                        # 设置为应用默认字体（样式表里也指定了 family，两者双保险）
                        QtWidgets.QApplication.setFont(QtGui.QFont(fams[0]))
                        return fams[0]
    except Exception:
        pass
    return None


def apply_shadow(widget, radius=18, color=QtGui.QColor(17, 17, 17, 40), offset=(0, 6)):
    effect = QtWidgets.QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(radius)
    effect.setColor(color)
    effect.setOffset(*offset)
    widget.setGraphicsEffect(effect)


class UnitListWidget(QtWidgets.QListWidget):
    orderChanged = QtCore.pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("unitList")
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.setDropIndicatorShown(True)

    def dropEvent(self, e: QtGui.QDropEvent):
        super().dropEvent(e)
        # 强制把“所有单元”放回第 0 行
        names = [self.item(i).text() for i in range(self.count())]
        if "所有单元" in names and names[0] != "所有单元":
            idx = names.index("所有单元")
            it = self.takeItem(idx)
            self.insertItem(0, it)
        # 发出顺序变化（不含“所有单元”）
        names = [self.item(i).text().strip() for i in range(self.count()) if i != 0]
        self.orderChanged.emit(names)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = init_db()
        self.setWindowTitle("LANSGANBS")
        self.resize(1180, 760)
        self.setMinimumSize(980, 620)

        # —— 设置中心 ——
        self.settings = SettingsManager(SETTINGS_PATH).load()

        # 每日一次自动备份（~/vocab_backups）
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.settings.get("last_auto_backup", "") != today:
                p = backup_db_file(DB_PATH)
                if p:
                    self.settings.set("last_auto_backup", today)
                    self.settings.save()
        except Exception:
            pass

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        # 顶部菜单：其他 → 平假名→片假名 测验
        m_other = self.menuBar().addMenu("其他")
        act_kana = m_other.addAction("平假名 → 片假名 测验")
        act_kana.triggered.connect(self.open_kana_quiz)

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # —— 设置中心 ——
        self.settings = SettingsManager(SETTINGS_PATH).load()

        # 顶部标题栏（不再包含导入/导出/主题切换）
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("多邻国日语词汇录入")
        title.setObjectName("appTitle")
        header.addWidget(title)
        header.addStretch()
        root.addLayout(header)

        # 先创建分割条并放入根布局
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        root.addWidget(splitter, 1)

        # 左：单元列表
        left_frame = QtWidgets.QFrame()
        left_frame.setObjectName("panel")
        apply_shadow(left_frame)
        left_layout = QtWidgets.QVBoxLayout(left_frame)
        left_layout.setContentsMargins(14, 14, 14, 14)
        left_layout.setSpacing(10)

        lbl = QtWidgets.QLabel("单元列表")
        lbl.setObjectName("sectionTitle")
        left_layout.addWidget(lbl)

        # 单元搜索框（支持子串匹配；Enter 选中第一条；Esc 清空）
        self.unit_filter = QtWidgets.QLineEdit()
        self.unit_filter.setPlaceholderText("搜索单元（拼写/中文/日文子串均可；Enter 选中首个；Esc 清空）")
        self.unit_filter.setClearButtonEnabled(True)
        left_layout.addWidget(self.unit_filter)

        # 绑定：输入即过滤；回车选中第一条；Esc 清空
        self.unit_filter.textChanged.connect(self._filter_units)
        _s1 = QtWidgets.QShortcut(QtGui.QKeySequence("Return"), self.unit_filter)
        _s1.setContext(QtCore.Qt.WidgetShortcut)
        _s1.activated.connect(self._activate_first_visible_unit)

        _s2 = QtWidgets.QShortcut(QtGui.QKeySequence("Enter"), self.unit_filter)
        _s2.setContext(QtCore.Qt.WidgetShortcut)
        _s2.activated.connect(self._activate_first_visible_unit)

        QtWidgets.QShortcut(QtGui.QKeySequence("Esc"), self.unit_filter, activated=lambda: self.unit_filter.clear())

        self.unit_list = UnitListWidget()
        self.unit_list.orderChanged.connect(self._persist_unit_order)
        self.unit_list.itemClicked.connect(self.on_unit_clicked)
        self.unit_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        left_layout.addWidget(self.unit_list, 1)

        # 右键菜单：重命名单元
        self.unit_list.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.unit_list.customContextMenuRequested.connect(self._on_unit_list_menu)

        self._adhoc_units = set()  # 会话级临时单元名，未录入时也让左侧显示

        # 1) 左侧按钮：用 2×2 网格替换原来的 QHBoxLayout，并把“导出 CSV”改为“导出”
        btns_left = QtWidgets.QGridLayout()

        btn_new_unit = QtWidgets.QPushButton("新建单元")
        btn_new_unit.setObjectName("secondary")
        btn_new_unit.clicked.connect(self.create_unit_dialog)

        btn_refresh_units = QtWidgets.QPushButton("刷新单元")
        btn_refresh_units.setObjectName("secondary")
        btn_refresh_units.clicked.connect(self.refresh_units)

        self.btn_merge_study = QtWidgets.QPushButton("复习")
        self.btn_merge_study.setObjectName("secondary")
        self.btn_merge_study.clicked.connect(self.open_study_window)

        btn_export_csv = QtWidgets.QPushButton("导出")  # 原为“导出 CSV”
        btn_export_csv.setObjectName("secondary")
        btn_export_csv.clicked.connect(self.export_csv_dialog)

        btn_delete_unit = QtWidgets.QPushButton("删除单元")
        btn_delete_unit.setObjectName("miniDanger")
        btn_delete_unit.clicked.connect(self.delete_unit_dialog)

        # 统一尺寸策略，单元格内尽量铺满
        for b in (btn_new_unit, btn_refresh_units, self.btn_merge_study, btn_export_csv, btn_delete_unit):
            b.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            b.setMinimumHeight(34)

        # 2×2 网格摆放
        btns_left.addWidget(btn_new_unit, 0, 0)
        btns_left.addWidget(btn_refresh_units, 0, 1)
        btns_left.addWidget(self.btn_merge_study, 1, 0)
        btns_left.addWidget(btn_export_csv, 1, 1)
        btns_left.addWidget(btn_delete_unit, 2, 0, 1, 2)  # 跨两列
        btns_left.setHorizontalSpacing(8)
        btns_left.setVerticalSpacing(8)

        left_layout.addLayout(btns_left)

        # 2) 分割条和左侧宽度：去掉最大宽度限制，让用户可拖拽放大
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(6)
        left_frame.setMinimumWidth(180)
        # 去掉这一行或改为更大上限：
        # left_frame.setMaximumWidth(280)
        # 若需要保留上限，可调宽一些，例如：
        # left_frame.setMaximumWidth(360)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        # 初始尺寸略放大一些，避免两行按钮仍被截断
        QtCore.QTimer.singleShot(0, lambda: splitter.setSizes([280, max(700, self.width() - 280)]))

        splitter.addWidget(left_frame)

        # 顶部快速筛选
        self.filter_bar = QtWidgets.QLineEdit()
        self.filter_bar.setPlaceholderText("快速筛选（支持 假名/汉字/罗马音/释义；Esc 清空，Ctrl+F 聚焦）")
        self.filter_bar.textChanged.connect(lambda s: self._proxy.setQuery(s))
        # 放进表格上方
        # 假设你的右侧布局变量叫 right_col / center_col，以下任选其一替换为你的实际变量名
        try:
            right_col.addWidget(self.filter_bar)
        except Exception:
            try:
                center_col.addWidget(self.filter_bar)
            except Exception:
                # 若你用的是 QVBoxLayout 变量名不同，请将此行改为该布局变量
                pass

        # 快捷键：Ctrl+F 聚焦，Esc 清空
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+F"), self,
                            activated=lambda: (self.filter_bar.setFocus(), self.filter_bar.selectAll()))
        self.filter_bar.installEventFilter(self)

        # === 中间区域：改为使用 QSplitter 上下分割 ===
        # 原来的 center_frame 现在作为整个中间的大容器
        center_frame = QtWidgets.QFrame()
        center_frame.setObjectName("panel") # 保持 panel 样式
        # 去掉阴影，因为内部还要分层，或者保留阴影但改内部布局
        apply_shadow(center_frame)

        # 使用 QVBoxLayout 作为总布局
        center_main_layout = QtWidgets.QVBoxLayout(center_frame)
        center_main_layout.setContentsMargins(0, 0, 0, 0) # 贴边
        center_main_layout.setSpacing(0)

        # 创建一个垂直分割器
        v_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        v_splitter.setHandleWidth(8) # 拖拽条稍宽方便操作
        center_main_layout.addWidget(v_splitter)

        # --- 上半部分：添加/录入区 (放在一个 Widget 里) ---
        top_widget = QtWidgets.QWidget()
        top_layout = QtWidgets.QVBoxLayout(top_widget)
        top_layout.setContentsMargins(16, 16, 16, 16)
        top_layout.setSpacing(8)

        # 区块标题
        add_title = QtWidgets.QLabel("添加到题库")
        add_title.setObjectName("sectionTitle")
        top_layout.addWidget(add_title)

        # 录入表单区 (保持你原来的 Form 逻辑)
        add_row = QtWidgets.QHBoxLayout()
        add_row.setSpacing(12)

        form_col = QtWidgets.QFormLayout()
        form_col.setLabelAlignment(QtCore.Qt.AlignRight)
        form_col.setVerticalSpacing(8) # 稍微紧凑一点

        self.lbl_current_unit = QtWidgets.QLabel("（请在左侧选择单元）")
        self.lbl_current_unit.setObjectName("muted")
        form_col.addRow("单元", self.lbl_current_unit)

        self.add_term = QtWidgets.QLineEdit()
        form_col.addRow("假名", self.add_term)

        self.roma_ime = QtWidgets.QLineEdit()
        self.roma_ime.setPlaceholderText("连续输入罗马音，例如: shixyuxtsushin")
        form_col.addRow("罗马音(输入法)", self.roma_ime)

        # 候选按钮
        self._btn_kana_hira = QtWidgets.QPushButton("")
        self._btn_kana_kata = QtWidgets.QPushButton("")
        for b in (self._btn_kana_hira, self._btn_kana_kata):
            b.setEnabled(False); b.setAutoDefault(False); b.setDefault(False)
        _btnrow = QtWidgets.QWidget()
        _btnrow_h = QtWidgets.QHBoxLayout(_btnrow)
        _btnrow_h.setContentsMargins(0,0,0,0); _btnrow_h.setSpacing(6)
        _btnrow_h.addWidget(self._btn_kana_hira); _btnrow_h.addWidget(self._btn_kana_kata)
        form_col.addRow("候选", _btnrow)

        self.add_kanji = QtWidgets.QLineEdit()
        form_col.addRow("汉字写法(可选)", self.add_kanji)

        self.add_kana = QtWidgets.QLineEdit()
        form_col.addRow("罗马音(可选)", self.add_kana)

        self.add_mean = QtWidgets.QLineEdit()
        form_col.addRow("中文释义", self.add_mean)

        # 信号绑定 (保持不变)
        self._term_autofilled = True; self._kanji_autofilled = True
        self._kana_autofilled = True; self._meaning_autofilled = True
        self._auto_kana_in_progress = False; self._last_auto_romaji = ""

        self.add_term.textEdited.connect(lambda _=None: setattr(self, "_term_autofilled", False))
        self.add_kanji.textEdited.connect(lambda _=None: setattr(self, "_kanji_autofilled", False))
        self.add_kana.textEdited.connect(self._on_add_kana_edited)
        self.add_mean.textEdited.connect(lambda _=None: setattr(self, "_meaning_autofilled", False))
        self.add_term.textChanged.connect(self._auto_fill_kana_from_term)

        # 定时器 (保持不变)
        self._mean_timer = QtCore.QTimer(self); self._mean_timer.setSingleShot(True)
        self._mean_timer.timeout.connect(self._auto_fill_meaning_from_term)
        self._mean2jp_timer = QtCore.QTimer(self); self._mean2jp_timer.setSingleShot(True)
        self._mean2jp_timer.timeout.connect(self._auto_suggest_from_zh)

        # 回车事件 (保持不变)
        self.add_term.returnPressed.connect(lambda: self._on_enter_in_field("term"))
        self.add_kanji.returnPressed.connect(lambda: self._on_enter_in_field("kanji"))
        self.add_kana.returnPressed.connect(lambda: self._on_enter_in_field("roma"))
        self.add_mean.returnPressed.connect(lambda: self._on_enter_in_field("zh"))

        # 弹窗初始化 (保持不变)
        self._cand_popup = CandidatePopup(self)
        self._cand_popup.candidate_chosen.connect(self._apply_candidate_from_popup)
        self._zh_prefer = dict(self.settings.get("zh_prefer", {}))
        self._kana_popup = KanaCandidatePopup(self)
        self._kana_popup.kana_chosen.connect(self._apply_kana_choice)
        self.add_term.textEdited.connect(self._maybe_show_kana_popup)

        # 罗马音输入法绑定 (保持不变)
        self.roma_ime.textEdited.connect(self._on_roma_ime_edited)
        self.roma_ime.returnPressed.connect(self._commit_roma_enter)
        self._btn_kana_hira.clicked.connect(lambda: self._apply_kana_choice(self._btn_kana_hira.text()))
        self._btn_kana_kata.clicked.connect(lambda: self._apply_kana_choice(self._btn_kana_kata.text()))

        # 补全 Completer (保持不变 - 若你用了 try...except 包裹这里也一样)
        try:
            # ... (Completer 代码可以复用你原来的，这里省略以节省篇幅，逻辑完全一致) ...
            pass
        except: pass

        add_row.addLayout(form_col, 1)

        # 右侧按钮列
        btncol = QtWidgets.QVBoxLayout()
        self.btn_add = QtWidgets.QPushButton("添加到题库")
        self.btn_add.setObjectName("primary") # 用蓝色主按钮更显眼
        self.btn_add.clicked.connect(self.add_card_from_form)
        self.btn_add.setMinimumHeight(36) # 加高一点
        btncol.addWidget(self.btn_add)
        btncol.addStretch()
        add_row.addLayout(btncol)

        top_layout.addLayout(add_row)

        # 将 top_widget 加入分割器
        v_splitter.addWidget(top_widget)

        # --- 下半部分：表格区 (放在另一个 Widget 里) ---
        bottom_widget = QtWidgets.QWidget()
        bottom_layout = QtWidgets.QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(16, 0, 16, 16) # 上方 0，因为分割条有间距
        bottom_layout.setSpacing(8)

        # 提示 & 工具栏
        self.center_hint = QtWidgets.QLabel("请选择左侧单元查看该单元的词条，或选择“所有单元”")
        self.center_hint.setObjectName("muted")
        bottom_layout.addWidget(self.center_hint)

        tools_bar = QtWidgets.QHBoxLayout()
        tools_bar.setSpacing(6)

        self.overview = QtWidgets.QWidget() # 占位兼容
        self.ov_count = QtWidgets.QLabel(); self.ov_review = QtWidgets.QLabel(); self.ov_recent = QtWidgets.QLabel() # 占位

        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.addItems(["默认顺序", "添加时间", "上次复习", "重复次数", "易度EF"])
        self.sort_combo.setFixedHeight(30) # 稍微加高适应字体
        self._beautify_combo(self.sort_combo) # 后面会定义这个美化函数
        tools_bar.addWidget(self.sort_combo)

        self.btn_shuffle = QtWidgets.QPushButton("打乱顺序")
        self.btn_shuffle.setObjectName("mini")
        self.btn_shuffle.setFixedHeight(30)
        tools_bar.addWidget(self.btn_shuffle)

        self.btn_overview = QtWidgets.QPushButton("总览")
        self.btn_overview.setObjectName("mini")
        self.btn_overview.setFixedHeight(30)
        self.btn_overview.clicked.connect(self._open_unit_overview)
        tools_bar.addWidget(self.btn_overview)

        tools_bar.addStretch()

        self.search_box = QtWidgets.QLineEdit()
        self.search_box.setPlaceholderText("搜索...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(200)
        tools_bar.addWidget(self.search_box)

        bottom_layout.addLayout(tools_bar)

        # 表格
        self.unit_table = QtWidgets.QTableView()
        self._card_model = CardTableModel(self)
        self._proxy = CardSortProxy(self)
        self._proxy.setSourceModel(self._card_model)
        self.unit_table.setModel(self._proxy)

        # 表格样式微调
        self.unit_table.verticalHeader().setVisible(False)
        self.unit_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.unit_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.unit_table.setAlternatingRowColors(True) # 夜间模式靠 CSS 修正
        self.unit_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

        # 表头
        header = self.unit_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setDefaultAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        header.setHighlightSections(False)
        # 列宽模式
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents) # ID
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)          # 假名
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents) # 汉字
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents) # 罗马音
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)          # 释义
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.Fixed)            # 操作
        self.unit_table.setColumnWidth(5, 90) # 稍微宽一点放按钮

        bottom_layout.addWidget(self.unit_table)

        # 将 bottom_widget 加入分割器
        v_splitter.addWidget(bottom_widget)

        # 设置分割器初始比例：上半部分占小一点，下半部分占大一点 (例如 300px : 剩余)
        v_splitter.setSizes([320, 600])
        v_splitter.setCollapsible(0, False) # 上半部分不能完全折叠
        v_splitter.setCollapsible(1, False)

        # 最终将 center_frame (也就是包含 splitter 的面板) 加入主分割器
        splitter.addWidget(center_frame)

        self._suppress_cand_for = ""  # 记住“当前不再自动弹候选”的中文文本

        # UI 初始化
        self.refresh_units()
        btn_new_unit.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogNewFolder))
        btn_refresh_units.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload))
        self.btn_add.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogApplyButton))
        menubar = self.menuBar() if hasattr(self, "menuBar") else None
        if menubar:
            m_file = menubar.addMenu("文件")
            act_export = QtWidgets.QAction("导出为 CSV...", self);
            act_export.triggered.connect(self.on_export_csv)
            act_import = QtWidgets.QAction("导入 CSV...", self);
            act_import.triggered.connect(self.on_import_csv)
            m_file.addAction(act_export)
            m_file.addAction(act_import)
            m_file.addSeparator()
            act_backup = QtWidgets.QAction("备份数据库", self);
            act_backup.triggered.connect(self.on_backup_db)
            act_restore = QtWidgets.QAction("从备份恢复...", self);
            act_restore.triggered.connect(self.on_restore_db)
            act_openbk = QtWidgets.QAction("打开备份目录", self);
            act_openbk.triggered.connect(self.on_open_backup_dir)
            m_file.addAction(act_backup)
            m_file.addAction(act_restore)
            m_file.addAction(act_openbk)
        else:
            # 没有菜单栏就加一个工具栏
            tb = self.addToolBar("文件")
            btn_export = QtWidgets.QAction("导出CSV", self);
            btn_export.triggered.connect(self.on_export_csv);
            tb.addAction(btn_export)
            btn_import = QtWidgets.QAction("导入CSV", self);
            btn_import.triggered.connect(self.on_import_csv);
            tb.addAction(btn_import)
            tb.addSeparator()
            btn_backup = QtWidgets.QAction("备份", self);
            btn_backup.triggered.connect(self.on_backup_db);
            tb.addAction(btn_backup)
            btn_restore = QtWidgets.QAction("恢复", self);
            btn_restore.triggered.connect(self.on_restore_db);
            tb.addAction(btn_restore)
            btn_openbk = QtWidgets.QAction("备份目录", self);
            btn_openbk.triggered.connect(self.on_open_backup_dir);
            tb.addAction(btn_openbk)
        # 设置菜单/工具栏
        if hasattr(self, "menuBar") and self.menuBar():
            m_edit = self.menuBar().addMenu("设置")
            act_settings = QtWidgets.QAction("设置…", self)
            act_settings.triggered.connect(self.open_settings_dialog)
            m_edit.addAction(act_settings)
        else:
            tb = self.addToolBar("设置")
            btn_settings = QtWidgets.QAction("设置…", self)
            btn_settings.triggered.connect(self.open_settings_dialog)
            tb.addAction(btn_settings)

        # 统计菜单
        if hasattr(self, "menuBar") and self.menuBar():
            m_stats = self.menuBar().addMenu("统计")
            act_stats = QtWidgets.QAction("学习统计…", self)
            act_stats.triggered.connect(self.open_stats_dialog)
            m_stats.addAction(act_stats)
        else:
            tb2 = self.addToolBar("统计")
            btn_stats = QtWidgets.QAction("学习统计…", self)
            btn_stats.triggered.connect(self.open_stats_dialog)
            tb2.addAction(btn_stats)

        # —— 其他菜单：平→片假名测验 ——
        if hasattr(self, "menuBar") and self.menuBar():
            m_misc = self.menuBar().addMenu("其他")
            act_kana = QtWidgets.QAction("平→片假名测验…", self)
            act_kana.triggered.connect(self.open_kana_quiz)
            m_misc.addAction(act_kana)
        else:
            tb_misc = self.addToolBar("其他")
            btn_kana = QtWidgets.QAction("平→片假名测验…", self)
            btn_kana.triggered.connect(self.open_kana_quiz)
            tb_misc.addAction(btn_kana)

        # 键盘快捷（space 作继续）
        self.shortcut_space = QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self)
        self.shortcut_space.activated.connect(self.on_space_pressed)

        # --- 在 MainWindow.__init__ 末尾附近（UI 初始化后）追加状态与信号 ---
        self._current_unit = None
        self._current_rows_all = []  # 原始（当前单元）
        self._current_rows_view = []  # 过滤/排序/打乱后的“当前显示”
        self._shuffled = False

        # —— 新增：测验窗口句柄（单例）
        self._kana_quiz_window = None

        # 绑定信号
        self.search_box.textChanged.connect(self._apply_filters_and_refresh)
        self.sort_combo.currentIndexChanged.connect(self._apply_filters_and_refresh)
        self.btn_shuffle.clicked.connect(self._on_shuffle_clicked)

        # 在 __init__ 末尾附近加入（若你已有就忽略）
        self._term_autofilled = True
        self._kanji_autofilled = True
        self._kana_autofilled = True  # 这里代表“罗马音”的自动填充标志
        self._meaning_autofilled = True

        self.add_term.textEdited.connect(lambda _=None: setattr(self, "_term_autofilled", False))
        self.add_kanji.textEdited.connect(lambda _=None: setattr(self, "_kanji_autofilled", False))
        self.add_kana.textEdited.connect(lambda _=None: setattr(self, "_kana_autofilled", False))
        self.add_mean.textEdited.connect(lambda _=None: setattr(self, "_meaning_autofilled", False))
        self.add_mean.textEdited.connect(lambda _=None: setattr(self, "_suppress_cand_for", ""))

        # === 新增功能：初始化 TTS ===
        self.speech = QtTextToSpeech.QTextToSpeech()
        # 尝试优先设置日语引擎
        found_ja = False
        for locale in self.speech.availableLocales():
            if locale.name().startswith("ja"):
                self.speech.setLocale(locale)
                found_ja = True
                break
        if not found_ja:
            print("[MainWindow] Warning: No Japanese TTS voice found. Using default.")

        # (注意：这里绝对不能有 self.prepare_queue()，那是 StudyWindow 里的)

        # 2. 状态栏增加夜间模式切换
        self.status_bar = self.statusBar()
        self.btn_theme = QtWidgets.QPushButton("🌙 夜间模式")
        self.btn_theme.setCheckable(True)
        self.btn_theme.clicked.connect(self.toggle_theme)
        # 设为扁平样式放入状态栏
        self.btn_theme.setStyleSheet("border:none; background:transparent; font-weight:bold;")
        self.status_bar.addPermanentWidget(self.btn_theme)

    # === 新增：TTS 发音方法 ===
    # 确认 MainWindow 类里有这个方法
    def speak_text(self, text):
        if not text: return
        # 修复：先检查对象是否存在，防止初始化失败导致崩溃
        if not hasattr(self, 'speech') or self.speech is None:
            return
        if self.speech.state() == QtTextToSpeech.QTextToSpeech.Speaking:
            self.speech.stop()
        self.speech.say(text)

    def toggle_theme(self, checked):
        app = QtWidgets.QApplication.instance()
        if checked:
            self.btn_theme.setText("☀️ 日间模式")

            # 1. 获取当前字体的 CSS (作为基础)
            real_font = self.font().family()
            base_css = get_app_style(real_font)

            # 2. 定义夜间模式覆盖层 (Dark Mode Overlay)
            # 增加了 QDialog, QTableCornerButton 的支持
            dark_css = """
            /* 全局深色背景 */
            QMainWindow, QWidget, QDialog { 
                background-color: #111827; 
                color: #e5e7eb; 
            }
            
            /* 面板背景 */
            QFrame#panel { background-color: #1f2937; border-color: #374151; }
            
            /* 文字颜色 */
            QLabel#appTitle, QLabel#sectionTitle, QLabel#bigterm { color: #f3f4f6; }
            QLabel, QCheckBox, QRadioButton { color: #e5e7eb; }
            QLabel#muted { color: #9ca3af; }
            
            /* 输入框 */
            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox { 
                background-color: #374151; 
                color: #f3f4f6; 
                border: 1px solid #4b5563; 
                selection-background-color: #2563eb;
            }
            
            /* 表格与列表 (核心修复) */
            QListWidget, QTableWidget, QTableView { 
                background-color: #1f2937; 
                color: #f3f4f6; 
                border: 1px solid #374151; 
                alternate-background-color: #111827; /* 偶数行深色，修复白色条纹 */
                gridline-color: #374151;
            }
            QListWidget::item:selected, QTableWidget::item:selected, QTableView::item:selected {
                background-color: #1e40af; 
                color: #ffffff;
            }
            /* 表格左上角空白块修复 */
            QTableCornerButton::section {
                background-color: #374151;
                border: 1px solid #4b5563;
            }
            
            /* 表头 */
            QHeaderView::section { 
                background-color: #374151; 
                color: #d1d5db; 
                border: none;
                border-bottom: 1px solid #4b5563; 
                border-right: 1px solid #4b5563;
            }
            
            /* 按钮 */
            QPushButton { background-color: #374151; color: #e5e7eb; border: 1px solid #4b5563; }
            QPushButton:hover { background-color: #4b5563; }
            
            QPushButton#primary { background-color: #2563eb; color: white; border: 1px solid #2563eb; }
            QPushButton#primary:hover { background-color: #1d4ed8; }
            
            QPushButton#miniDanger { background-color: #7f1d1d; color: #fca5a5; border-color: #7f1d1d; }
            QPushButton#miniDanger:hover { background-color: #991b1b; }
            
            /* 滚动条美化 (可选，防止原生白色滚动条太刺眼) */
            QScrollBar:vertical {
                border: none;
                background: #111827;
                width: 10px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #4b5563;
                min-height: 20px;
                border-radius: 5px;
            }
            """
            # 应用全局样式
            app.setStyleSheet(base_css + dark_css)
        else:
            self.btn_theme.setText("🌙 夜间模式")
            # 恢复日间模式
            real_font = self.font().family()
            app.setStyleSheet(get_app_style(real_font))

    def _filter_units(self, text: str):
        q = (text or "").strip().lower()
        # 逐项隐藏/显示，但不改变原有排序与拖拽顺序
        for i in range(self.unit_list.count()):
            it = self.unit_list.item(i)
            name = (it.text() or "").strip()
            if not q:
                it.setHidden(False)
                continue
            show = (q in name.lower())
            # “所有单元”仅在匹配时显示；不强制置顶（保持原顺序）
            it.setHidden(not show)

        # 若当前选中项被隐藏，则自动选中第一条可见项
        cur = self.unit_list.currentItem()
        if cur and cur.isHidden():
            self._activate_first_visible_unit()

    def _activate_first_visible_unit(self):
        for i in range(self.unit_list.count()):
            it = self.unit_list.item(i)
            if it and not it.isHidden():
                self.unit_list.setCurrentItem(it)
                self.on_unit_clicked(it)
                break

    def _persist_unit_order(self, names: list[str]):
        # names 已不含“所有单元”
        try:
            self.settings.set("unit_order", names or [])
            self.settings.save()
        except Exception:
            pass

    def _on_enter_in_field(self, which: str):
        """
        只在按回车时执行的补全/回填。
        - 'zh'  : 中文释义 → 根据本地词典做候选；唯一命中则直接应用；多候选则弹窗等待选择
        - 'term': 假名     → 回填中文/罗马音（强制），不在输入中自动抢填
        - 'kanji': 汉字写法 → 同上
        - 'roma': 罗马音   → 如你实现支持，也在此触发；否则可忽略
        """
        which = (which or "").lower()

        # 中文释义：只在按回车时检查/弹候选，不在输入期自动弹/自动改
        if which == "zh":
            text = (self.add_mean.text() or "").strip()
            # 若候选弹窗可见：按 Enter 视为“放弃候选、关闭弹窗并记住不再弹出”
            try:
                if getattr(self, "_cand_popup", None) and self._cand_popup.isVisible():
                    self._cand_popup.hide()
                    self._suppress_cand_for = text
                    return
            except Exception:
                pass
            # 否则按旧逻辑：尝试做一次自动候选（唯一命中则回填，多候选弹出）
            try:
                self._auto_suggest_from_zh()
            except Exception:
                pass
            return

        # 假名/汉字/罗马音：只在按回车时，一次性做最终回填
        if which in ("term", "kanji", "roma"):
            # 若都空就不做
            term = (self.add_term.text() or "").strip()
            kanji = (self.add_kanji.text() or "").strip()
            roma = (self.add_kana.text() or "").strip()
            if not (term or kanji or roma):
                return
            try:
                # 你已有的方法：基于 term/kanji/roma → 计算/查词典 → 回填中文/罗马音等
                # force=True 确保之前的临时值会被“最终值”覆盖
                self._auto_fill_meaning_from_term(force=True)
            except Exception:
                pass
            # 回车后，把焦点放到释义，便于马上确认/保存
            try:
                self.add_mean.setFocus()
                self.add_mean.selectAll()
            except Exception:
                pass

    def eventFilter(self, obj, e):
        if hasattr(self, "filter_bar") and obj is self.filter_bar:
            if e.type() == QtCore.QEvent.KeyPress and e.key() == QtCore.Qt.Key_Escape:
                self.filter_bar.clear()
                return True
        return super().eventFilter(obj, e)

    def _on_unit_list_menu(self, pos: QtCore.QPoint):
        item = self.unit_list.itemAt(pos) or self.unit_list.currentItem()
        menu = QtWidgets.QMenu(self)
        act_rename = menu.addAction("重命名单元…")
        chosen = menu.exec_(self.unit_list.mapToGlobal(pos))
        if chosen != act_rename or not item:
            return

        old = (item.text() or "").strip()
        if old == "所有单元":
            QtWidgets.QMessageBox.warning(self, "无法重命名", "请先选中一个具体单元。")
            return

        new, ok = QtWidgets.QInputDialog.getText(self, "重命名单元", f"将单元“{old}”重命名为：", text=old)
        new = (new or "").strip()
        if (not ok) or (not new) or (new == old):
            return

        # 若目标名已存在，直接提示
        names = [self.unit_list.item(i).text().strip() for i in range(self.unit_list.count())]
        if new in names:
            QtWidgets.QMessageBox.warning(self, "重名", f"已存在名为「{new}」的单元。")
            return

        try:
            n = count_cards_in_unit(self.db, old)
            if n > 0:
                # DB 中确有记录：更新 DB
                cur = self.db.cursor()
                cur.execute("UPDATE cards SET unit=? WHERE unit=?", (new, old))
                self.db.commit()
            else:
                # 空单元：只在左侧列表与会话临时集合中存在
                if hasattr(self, "_adhoc_units"):
                    if old in self._adhoc_units:
                        self._adhoc_units.discard(old)
                    self._adhoc_units.add(new)
            # 刷新列表并选中新名
            self.refresh_units()
            items = self.unit_list.findItems(new, QtCore.Qt.MatchExactly)
            if items:
                self.unit_list.setCurrentItem(items[0])
                self.on_unit_clicked(items[0])

            # 如果你已做“单元顺序持久化”，这里顺带替换顺序表里的名字
            try:
                order = list(self.settings.get("unit_order", []) or [])
                order = [new if x == old else x for x in order]
                self.settings.set("unit_order", order)
                self.settings.save()
            except Exception:
                pass

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "重命名失败", str(e))

    def on_import_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "导入 CSV", "", "CSV 文件 (*.csv);;所有文件 (*)"
        )
        if not path: return
        try:
            items = _read_csv_rows(path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "读取失败", f"读取 CSV 失败：{e}")
            return
        if not items:
            QtWidgets.QMessageBox.information(self, "空文件", "未读取到任何数据。")
            return

        # 选择重复处理策略
        policy, ok = QtWidgets.QInputDialog.getItem(
            self, "重复处理",
            "遇到同单元且假名/汉字相同的词条：",
            ["跳过(默认)", "覆盖原记录", "并存(新建一条)"], 0, False
        )
        if not ok: return

        # 可选：统一导入到“当前单元”
        use_current, ok2 = QtWidgets.QInputDialog.getItem(
            self, "单元归属",
            "导入到：", ["按CSV中的单元", "当前选中单元"], 0, False
        )
        if not ok2: return
        fixed_unit = None
        if use_current == "当前选中单元":
            it = self.unit_list.currentItem()
            if it:
                name = it.text().strip()
                fixed_unit = None if name == "所有单元" else name
            if not fixed_unit:
                QtWidgets.QMessageBox.warning(self, "未选择单元", "请先在左侧选择一个具体单元。")
                return

        # 逐条导入
        n_ok, n_dup, n_new = 0, 0, 0
        for d in items:
            unit = fixed_unit if fixed_unit else d.get("unit", "").strip()
            kana = d.get("kana", "").strip()
            kanji = d.get("kanji", "").strip()
            romaji = d.get("romaji", "").strip()
            mean = d.get("meaning", "").strip()

            # 最小字段：至少要有 假名 或 汉字 或 释义 之一
            if not (kana or kanji or mean):
                continue
            if not unit:
                # 没单元则缺省为“未分组”
                unit = "未分组"

            # 查重
            dup_id = _find_duplicate_card(self.db, unit, kana, kanji)
            if dup_id is not None:
                n_dup += 1
                if policy.startswith("跳过"):
                    continue
                elif policy.startswith("覆盖"):
                    try:
                        # 你已有的“全字段更新”函数名若不同，请替换为你自己的
                        update_card_fields_full(
                            self.db, dup_id, "日语", unit,
                            kana or kanji or "",  # term: 你的工程里 term=假名；缺时退回kanji
                            mean, kanji, romaji, ""
                        )
                        n_ok += 1
                    except Exception:
                        pass
                    continue
                else:
                    # 并存：落到新增逻辑
                    pass

            # 新增
            try:
                # 你的新增方法若名不同，替换即可；term=假名，没有假名用汉字兜底
                add_card(self.db, "日语", unit, kana or kanji or "", mean,
                         jp_kanji=kanji, jp_kana=romaji)
                n_ok += 1;
                n_new += 1
            except Exception:
                pass

        # 刷新当前视图
        it = self.unit_list.currentItem()
        if it:
            self.on_unit_clicked(it)
        else:
            self.refresh_units()

        QtWidgets.QMessageBox.information(
            self, "导入完成",
            f"导入成功：{n_ok} 条\n其中：覆盖/更新 {n_ok - n_new} 条，新建 {n_new} 条，遇到重复 {n_dup} 条。"
        )

    def on_backup_db(self):
        dst = backup_db_file(DB_PATH)
        if dst:
            QtWidgets.QMessageBox.information(self, "备份成功", f"已备份到：\n{dst}")
        else:
            QtWidgets.QMessageBox.critical(self, "备份失败", "无法创建备份文件。")

    def on_restore_db(self):
        _ensure_dir(BACKUP_DIR)
        start_dir = BACKUP_DIR if os.path.exists(BACKUP_DIR) else os.path.dirname(DB_PATH)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择备份文件恢复", start_dir, "数据库文件 (*.db);;所有文件 (*)"
        )
        if not path: return

        # 二次确认
        reply = QtWidgets.QMessageBox.question(
            self, "确认恢复",
            "将用所选备份覆盖当前数据库，当前未保存的数据将丢失。是否继续？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        try:
            # 关闭现有连接
            try:
                self.db.close()
            except Exception:
                pass
            # 覆盖数据库文件
            shutil.copy2(path, DB_PATH)
            # 重新打开并初始化（含迁移/索引）
            self.db = init_db(DB_PATH)
            # 刷新 UI
            self.refresh_units()
            QtWidgets.QMessageBox.information(self, "恢复完成", "数据库已从备份恢复。")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "恢复失败", f"恢复时出错：{e}")

    def on_open_backup_dir(self):
        _ensure_dir(BACKUP_DIR)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(BACKUP_DIR))

    def on_export_csv(self):
        # 选择导出范围
        scope, ok = QtWidgets.QInputDialog.getItem(
            self, "导出范围", "请选择：", ["当前单元", "所有单元"], 0, False
        )
        if not ok: return

        # 取数据
        cur_item = self.unit_list.currentItem()
        cur_unit = None
        if scope == "当前单元" and cur_item:
            name = cur_item.text().strip()
            cur_unit = None if name == "所有单元" else name

        try:
            if cur_unit is None:
                rows = list_cards_by_unit(self.db, None)
                title = "所有单元"
            else:
                rows = list_cards_by_unit(self.db, cur_unit)
                title = cur_unit
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", f"查询数据失败：{e}")
            return

        if not rows:
            QtWidgets.QMessageBox.information(self, "无数据", "没有可导出的词条。")
            return

        # 选择文件名
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"导出_{title}_{ts}.csv"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出为 CSV", default_name, "CSV 文件 (*.csv);;所有文件 (*)"
        )
        if not path: return

        # 组织导出列（与 CSV_HEADERS 对齐）
        out = []
        for r in rows:
            # r 下标：0=id, 3=kana(term), 11=kanji, 12=romaji(jp_kana), 4=meaning
            out.append([
                r[2] if len(r) > 2 else "",  # unit（你的查询若把 unit 放在 r[2]）
                (r[3] or "") if len(r) > 3 else "",  # kana
                (r[11] or "") if len(r) > 11 else "",  # kanji
                (r[12] or "") if len(r) > 12 else "",  # romaji（你库里 jp_kana 实际放罗马音）
                (r[4] or "") if len(r) > 4 else "",  # meaning
                (r[5] or "") if len(r) > 5 else "",  # created_at
                (r[6] or "") if len(r) > 6 else "",  # last_review
                (r[7] or "") if len(r) > 7 else "",  # interval
                (r[8] or "") if len(r) > 8 else "",  # repetition
                (r[9] or "") if len(r) > 9 else "",  # ef
                (r[10] or "") if len(r) > 10 else "",  # due_date
                str(r[0] or "")  # id
            ])

        try:
            _write_csv_rows(path, CSV_HEADERS, out)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", f"写文件失败：{e}")
            return

        QtWidgets.QMessageBox.information(self, "完成", f"已导出 {len(out)} 条到：\n{path}")

    def _rank_strings(self, all_list, q: str):
        q = (q or "").strip()
        if not q:
            return all_list[:]
        exact = []
        prefix = []
        contain = []
        others = []
        q_low = q.lower()
        seen = set()
        for s in all_list:
            if not s:
                continue
            key = s  # 保持大小写/原样
            if key in seen:
                continue
            seen.add(key)
            s_low = s.lower()
            if s_low == q_low:
                exact.append(s)
            elif s_low.startswith(q_low):
                prefix.append(s)
            elif q_low in s_low:
                contain.append(s)
            else:
                others.append(s)
        return exact + prefix + contain + others

    def _reorder_kana_model(self, text: str):
        if hasattr(self, "_kana_model"):
            self._kana_model.setStringList(self._rank_strings(self._kana_all, text))

    def _reorder_kanji_model(self, text: str):
        if hasattr(self, "_kanji_model"):
            self._kanji_model.setStringList(self._rank_strings(self._kanji_all, text))

    def _is_kana_only(self, s: str) -> bool:
        if not s: return False
        for ch in s:
            code = ord(ch)
            if not (0x3040 <= code <= 0x30FF):  # 平/片假名
                return False
        return True

    def _apply_candidate_tuple(self, cand: tuple[str, str, str], force: bool = False):
        """
        根据候选 (term, kana, meaning) 回填到“假名/汉字/罗马音/释义”。
        尊重手动输入优先级：只有在目标框为空或此前由自动填充时才覆盖。
        """
        term, kana, meaning = cand
        # 1) 释义：不覆盖用户正在输入的中文
        # if force and meaning: self.add_mean.setText(meaning)

        # 2) 汉字写法：仅当 term 含汉字
        if term and any(0x4E00 <= ord(c) <= 0x9FFF for c in term):
            cur = (self.add_kanji.text() or "").strip()
            if force or not cur or getattr(self, "_kanji_autofilled", True):
                if not cur or cur != term:
                    self.add_kanji.setText(term)
                    self._kanji_autofilled = True

        # 3) 假名：优先使用“看起来是日文假名”的内容；如果 kana 不是假名但 term 是假名 → 用 term。
        def _looks_kana(s: str) -> bool:
            if not s: return False
            for ch in s:
                code = ord(ch)
                if ch.isspace():
                    continue
                if (0x3040 <= code <= 0x309F) or (0x30A0 <= code <= 0x30FF) or \
                        (0x31F0 <= code <= 0x31FF) or (0xFF66 <= code <= 0xFF9D) or \
                        ch in ("ー", "・"):
                    continue
                return False
            return True

        if _looks_kana(kana):
            kana_to_use = kana
        elif _looks_kana(term) or self._is_kana_only(term or ""):
            # 典型外来语：term=片假名, kana=英文 → 用 term 作为假名
            kana_to_use = term
        else:
            # 兜底：两者都不像假名 → 尽量不回填（保持空），避免把英文塞进假名框
            kana_to_use = ""

        if kana_to_use:
            cur = (self.add_term.text() or "").strip()
            if force or not cur or getattr(self, "_term_autofilled", True):
                if not cur or cur != kana_to_use:
                    self.add_term.setText(kana_to_use)
                    self._term_autofilled = True

            # 4) 罗马音：有假名时自动回填（不覆盖手动）
            try:
                romaji = kana_to_romaji(kana_to_use)  # 你工程已有此函数；若无则 try/except 保底
            except Exception:
                romaji = ""
            if romaji:
                cur_r = (self.add_kana.text() or "").strip()
                if force or not cur_r or getattr(self, "_kana_autofilled", True):
                    if not cur_r or cur_r != romaji:
                        self.add_kana.setText(romaji)
                        self._kana_autofilled = True

    def _apply_candidate_from_popup(self, cand: tuple[str, str, str], remember: bool):
        """
        候选弹窗回调：应用并可选择记住偏好。
        修复点：选择候选后，中文释义框也替换为所选候选的“标准中文”。
        """
        # 先把 term/kanji/kana/romaji 等统一回填（force=True 确保覆盖之前的自动填内容）
        self._apply_candidate_tuple(cand, force=True)

        # 关键：把“中文释义”输入框替换为候选里的中文文本
        try:
            mean = (cand[2] or "").strip()
            if mean:
                self.add_mean.blockSignals(True)
                self.add_mean.setText(mean)
                self._meaning_autofilled = True
        finally:
            self.add_mean.blockSignals(False)

        # 偏好记忆（保持你现有逻辑）
        zh = (self.add_mean.text() or "").strip()
        if remember and zh:
            self._zh_prefer[zh] = cand
            if self.settings.get("remember_zh_preference_persist", True):
                self.settings.set_zh_prefer(zh, cand)
                self.settings.save()

        # 选完把焦点给“假名”，便于继续录入（保持原行为）
        self.add_term.setFocus()

    def _auto_suggest_from_zh(self):
        """
        监听“中文释义”输入：单候选直接回填；多候选弹窗选择；
        如果用户之前对该中文“记过偏好”，则优先套用偏好。
        """
        zh = (self.add_mean.text() or "").strip()
        # 如果用户刚刚按 Enter 明确放弃了该内容的候选，不再弹出
        if zh == getattr(self, "_suppress_cand_for", ""):
            try:
                if getattr(self, "_cand_popup", None):
                    self._cand_popup.hide()
            finally:
                return

        if not zh:
            self._cand_popup.hide()
            return
        # 有记忆的偏好优先
        if zh in self._zh_prefer:
            self._apply_candidate_tuple(self._zh_prefer[zh], force=False)
            self._cand_popup.hide()
            return

        # 取候选
        try:
            cands = _LOCAL_DICT.search_by_meaning(zh, limit=8)
        except Exception:
            cands = []

        if not cands:
            self._cand_popup.hide()
            return

        # 避免与其它 Completer 弹窗重叠
        try:
            c = self.add_term.completer()
            if c and c.popup(): c.popup().hide()
            c2 = self.add_kanji.completer()
            if c2 and c2.popup(): c2.popup().hide()
        except Exception:
            pass

        if len(cands) == 1:
            # 单候选：在不覆盖手动输入的前提下直接回填
            self._apply_candidate_tuple(cands[0], force=False)
            self._cand_popup.hide()
        else:
            # 多候选：弹窗让用户选；Ctrl+回车/点击可“记住此选择”
            self._cand_popup.show_for(self.add_mean, zh, cands)

    # --- 统计 & 过滤 & 排序 ---
    def _calc_overview(self, rows):
        total = len(rows)
        cnt_kana = sum(1 for r in rows if (len(r) > 12 and (r[12] or "").strip()))
        cnt_kanji = sum(1 for r in rows if (len(r) > 11 and (r[11] or "").strip()))
        cnt_mean = sum(1 for r in rows if (r[4] or "").strip())
        cnt_reviewed = sum(1 for r in rows if (r[6] or "").strip())  # last_review 索引=6
        reps = [int(r[8] or 0) for r in rows]  # repetition 索引=8
        rep0 = sum(1 for x in reps if x == 0);
        rep1_2 = sum(1 for x in reps if 1 <= x <= 2);
        rep3p = sum(1 for x in reps if x >= 3)

        # 最近添加/复习
        def _safe_dt(s):
            from datetime import datetime
            try:
                return datetime.fromisoformat(s) if s else None
            except Exception:
                return None

        created = [_safe_dt(r[5]) for r in rows if (r[5] or "").strip()]  # created_at 索引=5
        reviewed = [_safe_dt(r[6]) for r in rows if (r[6] or "").strip()]  # last_review 索引=6
        last_add = max(created).strftime("%Y-%m-%d") if created else "—"
        last_rev = max(reviewed).strftime("%Y-%m-%d") if reviewed else "—"

        return {
            "total": total,
            # 无 fill_rate
            "cnt_reviewed": cnt_reviewed,
            "rep_bins": (rep0, rep1_2, rep3p),
            "last_add": last_add,
            "last_rev": last_rev,
            "cnt_kana": cnt_kana, "cnt_kanji": cnt_kanji, "cnt_mean": cnt_mean
        }

    def _apply_overview(self, stat):
        # 在“总词数”里顺便显示三项计数（不是百分比）
        self.ov_count.setText(
            f"{stat['total']} 词（假名 {stat['cnt_kana']}｜汉字 {stat['cnt_kanji']}｜释义 {stat['cnt_mean']}）")
        r0, r12, r3 = stat['rep_bins']
        self.ov_review.setText(f"已复习 {stat['cnt_reviewed']} 条｜重复 0:{r0} 1-2:{r12} 3+:{r3}")
        self.ov_recent.setText(f"最近 添加/复习：{stat['last_add']} / {stat['last_rev']}")

    def _filtered_rows(self):
        txt = (self.search_box.text() or "").strip().lower()
        rows = self._current_rows_all

        def _hit(r):
            if not txt:
                return True
            term = (r[3] or "").lower()
            mean = (r[4] or "").lower()
            kanji = (r[11] or "").lower() if len(r) > 11 and r[11] else ""
            kana = (r[12] or "").lower() if len(r) > 12 and r[12] else ""
            return (txt in term) or (txt in mean) or (txt in kanji) or (txt in kana)

        rows = [r for r in rows if _hit(r)]

        # 排序
        key = self.sort_combo.currentText()
        from datetime import datetime
        def _ts(s: str) -> float:
            """把各种可能的时间字符串统一成 float 时间戳；无效/空返回 0.0。"""
            if not s:
                return 0.0
            try:
                ss = s.strip().replace("T", " ").replace("Z", "")
                dt = datetime.fromisoformat(ss)  # 兼容 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS'
                return float(dt.timestamp())
            except Exception:
                return 0.0

        if key == "添加时间":
            rows.sort(key=lambda r: _ts(r[5] if len(r) > 5 else ""), reverse=True)  # created_at
        elif key == "上次复习":
            rows.sort(key=lambda r: _ts(r[6] if len(r) > 6 else ""), reverse=True)  # last_review
        elif key == "重复次数":
            rows.sort(key=lambda r: int(r[8] or 0), reverse=True)
        elif key == "易度EF":
            rows.sort(key=lambda r: float(r[9] or 0.0), reverse=True)

        # 打乱
        if getattr(self, "_shuffled", False):
            import random
            random.shuffle(rows)

        return rows

    def _apply_filters_and_refresh(self):
        self._current_rows_view = self._filtered_rows() if self._current_rows_all is not None else []
        try:
            self.populate_unit_table(self._current_rows_view)
        except Exception as e:
            print("[populate] failed:", e)
            # 退一步：清空表，避免直接崩溃
            self.populate_unit_table([])

    def _on_shuffle_clicked(self):
        self._shuffled = not self._shuffled
        self._apply_filters_and_refresh()

    # 3) 在 class MainWindow 内新增/替换以下方法
    def _selected_units(self):
        items = self.unit_list.selectedItems()
        seen, units = set(), []
        for it in items:
            t = (it.text() or "").strip()
            if t and t not in seen:
                units.append(t)
                seen.add(t)
        return units

    # 3) 替换 MainWindow._on_add_kana_edited，实现“仅在用户真正改动时关闭自动覆盖”
    def _on_add_kana_edited(self, _text: str):
        # 程序写入期间忽略
        if getattr(self, "_auto_kana_in_progress", False):
            return
        # 只有当与上次自动写入不同，才视为用户自定义，关闭后续自动覆盖
        last = getattr(self, "_last_auto_romaji", "")
        cur = (self.add_kana.text() or "").strip()
        self._kana_autofilled = (cur == "" or cur == last)

    def start_review_selected_units(self):
        # 统一走 open_study_window，从左侧选择读取
        self.open_study_window()

    def delete_unit_dialog(self):
        # 取选中单元：优先当前项，若多选则要求只选一项
        items = self.unit_list.selectedItems()
        if not items:
            cur_item = self.unit_list.currentItem()
            unit = cur_item.text() if cur_item else None
        elif len(items) == 1:
            unit = items[0].text()
        else:
            QtWidgets.QMessageBox.information(self, "提示", "一次只能删除一个单元，请只选择一个。")
            return

        if not unit or unit == "所有单元":
            QtWidgets.QMessageBox.information(self, "提示", "请选择具体单元。")
            return

        n = count_cards_in_unit(self.db, unit)
        ret = QtWidgets.QMessageBox.question(
            self, "确认删除",
            f"将删除单元「{unit}」及其中 {n} 条单词，操作不可恢复。\n是否继续？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No
        )
        if ret != QtWidgets.QMessageBox.Yes:
            return

        try:
            delete_unit(self.db, unit)
            # 从左侧列表移除，并清理临时单元集合
            matches = self.unit_list.findItems(unit, QtCore.Qt.MatchExactly)
            for it in matches:
                row = self.unit_list.row(it)
                self.unit_list.takeItem(row)
            if hasattr(self, "_adhoc_units"):
                self._adhoc_units.discard(unit)

            # 若正在查看该单元，则切回“所有单元”
            cur = self.unit_list.findItems("所有单元", QtCore.Qt.MatchExactly)
            if cur:
                self.unit_list.setCurrentItem(cur[0])
                self.on_unit_clicked(cur[0])
            QtWidgets.QMessageBox.information(self, "完成", f"已删除单元「{unit}」。")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "错误", f"删除失败：{e}")

    # 2) 在 class MainWindow 内新增：统一美化下拉框
    def _beautify_combo(self, cb: QtWidgets.QComboBox):
        view = QtWidgets.QListView(cb)
        view.setSpacing(2)
        view.setUniformItemSizes(True)
        cb.setView(view)
        cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        cb.setMinimumContentsLength(6)

    def open_study_window(self, unit_filter=None, include_all=True):
        # 若由按钮 clicked(bool) 触发，Qt 会把一个 bool 作为第一个参数传入，这里忽略之
        if isinstance(unit_filter, bool):
            unit_filter = None

        picked = unit_filter if unit_filter is not None else self._selected_units()
        if not picked:
            QtWidgets.QMessageBox.information(self, "提示", "请在左侧先选择至少一个单元。")
            return

        # 关闭已有复习窗口，避免复用旧过滤条件
        try:
            if getattr(self, "study_win", None) and self.study_win.isVisible():
                self.study_win.close()
        except Exception:
            pass
        self.study_win = None

        # 多选传列表，单选传字符串
        if isinstance(picked, (list, tuple, set)):
            unit_filter_final = list(dict.fromkeys([str(x).strip() for x in picked if str(x).strip()]))
            if len(unit_filter_final) == 1:
                unit_filter_final = unit_filter_final[0]
        else:
            unit_filter_final = picked

        try:
            self.study_win = StudyWindow(self.db, unit_filter=unit_filter_final, include_all=include_all)
            self.study_win.setMinimumSize(960, 640)
            self.study_win.resize(1200, 780)
            screen = QtWidgets.QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                fg = self.study_win.frameGeometry()
                fg.moveCenter(geo.center())
                self.study_win.move(fg.topLeft())
            self.study_win.destroyed.connect(lambda *_: setattr(self, "study_win", None))
            self.study_win.show()
            self.study_win.activateWindow()
            self.study_win.raise_()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "错误", f"启动复习窗口失败：{e}")

    # ---------- 单元管理 ----------
    def refresh_units(self):
        # 记住当前选中
        cur_text = self.unit_list.currentItem().text() if self.unit_list.currentItem() else None

        self.unit_list.clear()
        self.unit_list.addItem("所有单元")

        got = set(list_units(self.db))

        # 并入会话级临时单元（空单元也要显示/排序）
        if hasattr(self, "_adhoc_units"):
            got |= set(self._adhoc_units)

        # 读取已保存的顺序（不含“所有单元”）
        order = list(self.settings.get("unit_order", []) or [])

        # 用“已保存顺序在前，新增单元按字母顺序在后”的策略生成最终顺序
        ordered = []
        seen = set()
        for name in order:
            if name in got and name not in seen:
                ordered.append(name);
                seen.add(name)
        for name in sorted(got):
            if name not in seen:
                ordered.append(name);
                seen.add(name)

        # 写回列表
        for u in ordered:
            self.unit_list.addItem(u)

        self.refresh_units_to_combo()
        self.refresh_study_units()

        # 尝试恢复选择
        if cur_text:
            items = self.unit_list.findItems(cur_text, QtCore.Qt.MatchExactly)
            if items:
                self.unit_list.setCurrentItem(items[0])

        # 若当前没有任何选择，则默认选中“所有单元”并加载
        if not self.unit_list.currentItem():
            items = self.unit_list.findItems("所有单元", QtCore.Qt.MatchExactly)
            if items:
                self.unit_list.setCurrentItem(items[0])
                self.on_unit_clicked(items[0])

    # python
    def refresh_study_units(self):
        """
        兼容旧接口：当前学习窗口从左侧列表选择单元，
        无需单独刷新控件，这里留空避免报错。
        """
        pass

    def _auto_fill_from_zh(self, force: bool = False):
        """
        当用户在“中文释义”里输入时，尝试补全：假名（填到 self.add_term）、汉字写法（self.add_kanji）
        罗马音沿用你现有“假名→罗马音”自动逻辑（我们设置 term 后会触发）。
        """
        zh = (self.add_mean.text() or "").strip()
        if not zh:
            return

        e = suggest_from_zh_meaning(zh)  # -> (e_term, e_kana, e_mean) or None
        if not e:
            # 没命中就安静返回
            return

        e_term, e_kana, e_mean = e
        filled = []

        # 1) 补“汉字写法”：仅当 e_term 含汉字
        if e_term and any(0x4E00 <= ord(c) <= 0x9FFF for c in e_term):
            cur = (self.add_kanji.text() or "").strip()
            if force or not cur or getattr(self, "_kanji_autofilled", True):
                if not cur or cur != e_term:
                    self.add_kanji.setText(e_term)
                    self._kanji_autofilled = True
                    filled.append(f"kanji={e_term}")

        # 2) 补“假名”：优先用 e_kana；如果没有 kana 且 e_term 本身就是纯假名，也可回填
        def _is_pure_kana(s: str) -> bool:
            return _has_kana(s) and not _has_kanji(s)

        if e_kana:
            cur = (self.add_term.text() or "").strip()
            if force or not cur or getattr(self, "_term_autofilled", True):
                if not cur or cur != e_kana:
                    self.add_term.setText(e_kana)  # 这一步会自动触发“假名→罗马音”
                    self._term_autofilled = True
                    filled.append(f"term={e_kana}")
        elif e_term and _is_pure_kana(e_term):
            cur = (self.add_term.text() or "").strip()
            if force or not cur or getattr(self, "_term_autofilled", True):
                if not cur or cur != e_term:
                    self.add_term.setText(e_term)  # 触发“假名→罗马音”
                    self._term_autofilled = True
                    filled.append(f"term={e_term}")

        # 3) 中文释义：你已经在输入，不做覆盖（除非 force=True）
        # if force and e_mean:
        #     self.add_mean.setText(e_mean); self._meaning_autofilled = True

        if filled:
            print(f"[AutoFillZH] 命中: {zh} -> {', '.join(filled)}")
        else:
            print(f"[AutoFillZH] 命中但未覆盖（均视为手动输入）：{zh}")

    def refresh_units_to_combo(self):
        if not hasattr(self, 'add_unit_combo'):
            return  # 右侧已不提供主动选择
        units = list_units(self.db)
        cur = self.add_unit_combo.currentText()
        self.add_unit_combo.blockSignals(True)
        self.add_unit_combo.clear()
        for u in units:
            self.add_unit_combo.addItem(u)
        if cur:
            self.add_unit_combo.setCurrentText(cur)
        else:
            self.add_unit_combo.setEditText("")
        self.add_unit_combo.blockSignals(False)

    def create_unit_dialog(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "新建单元", "单元名：")
        if ok and text.strip():
            name = text.strip()

            # 左侧列表立即加入，并选中该单元
            names = [self.unit_list.item(i).text() for i in range(self.unit_list.count())]
            if name not in names:
                self.unit_list.addItem(name)
                self._adhoc_units.add(name)

            # 选中并触发加载（空单元仅显示总览/空表）
            items = self.unit_list.findItems(name, QtCore.Qt.MatchExactly)
            if items:
                self.unit_list.setCurrentItem(items[0])
                self.on_unit_clicked(items[0])

            QtWidgets.QMessageBox.information(self, "已设置", "已填入单元名，请继续添加词条。")

    def _restart_mean_timer(self):
        # 若正有“中文候选弹窗”在显示，先不要自动回填
        try:
            if getattr(self, "_cand_popup", None) and self._cand_popup.isVisible():
                return
        except Exception:
            pass

        # 若假名/汉字的 QCompleter 下拉正在显示，同样不要回填
        try:
            c = self.add_term.completer()
            if c and c.popup() and c.popup().isVisible():
                return
        except Exception:
            pass
        try:
            c2 = self.add_kanji.completer()
            if c2 and c2.popup() and c2.popup().isVisible():
                return
        except Exception:
            pass

        if hasattr(self, "_mean_timer") and self._mean_timer:
            self._mean_timer.stop()
            self._mean_timer.start(300)

    # 替换位置：class MainWindow 方法 _impl_auto_fill_kana_from_term
    def _impl_auto_fill_kana_from_term(self):
        term = (self.add_term.text() or "").strip()
        if not term:
            return

        # 仅对“只有假名、且无汉字”的词条自动转罗马音
        if not (_has_kana(term) and not _has_kanji(term)):
            return

        romaji = kana_to_romaji(term).strip()
        cur = (self.add_kana.text() or "").strip()
        last = getattr(self, "_last_auto_romaji", "")

        # 允许自动覆盖的条件：
        # 1) 未被用户自定义（_kana_autofilled 为 True）
        # 2) 或当前罗马音为空
        # 3) 或当前罗马音仍等于上次自动写入（说明未被用户改动）
        if getattr(self, "_kana_autofilled", True) or cur == "" or cur == last:
            self._auto_kana_in_progress = True
            try:
                self.add_kana.blockSignals(True)
                self.add_kana.setText(romaji)
                self._last_auto_romaji = romaji
                self._kana_autofilled = True
            finally:
                self.add_kana.blockSignals(False)
                self._auto_kana_in_progress = False

    def _auto_fill_kana_from_term(self):
        return self._impl_auto_fill_kana_from_term()

    # --- 2) 在 MainWindow 内替换 _auto_fill_meaning_from_term，并新增 _is_japanese_like ---
    # 放到 class MainWindow 中（__init__ 外部），保持现有 self._mean_timer 绑定不变
    def _is_japanese_like(self, s: str) -> bool:
        if not s:
            return False
        for ch in s:
            code = ord(ch)
            if (0x3040 <= code <= 0x30FF) or (0x31F0 <= code <= 0x31FF) or (0x4E00 <= code <= 0x9FFF):
                return True
        return False

    # 替换位置：class MainWindow 方法 _auto_fill_meaning_from_term
    def _auto_fill_meaning_from_term(self, force: bool = False):
        # 用户已手写且非强制，不覆盖
        if not force and (self.add_mean.text() or "").strip():
            return

        term = (self.add_term.text() or "").strip()
        kanji = (self.add_kanji.text() or "").strip()

        raw_keys = []
        if kanji: raw_keys.append(kanji)
        if term and term not in raw_keys: raw_keys.append(term)
        keys = [k for k in raw_keys if self._is_japanese_like(k)]
        if not keys:
            return

        hit = None
        hit_key = ""
        for k in keys:
            e = suggest_full_entry(k)  # (e_term, e_kana, e_mean)
            if e:
                hit = e
                hit_key = k
                break
        if not hit:
            print(f"[AutoFill] 未命中: {keys}")
            return

        e_term, e_kana, e_mean = hit
        filled = []

        # 填“汉字写法”：当命中项 term 含汉字时
        if e_term and any(0x4E00 <= ord(c) <= 0x9FFF for c in e_term):
            cur = (self.add_kanji.text() or "").strip()
            if force or not cur or getattr(self, "_kanji_autofilled", True):
                if not cur or cur != e_term:
                    self.add_kanji.setText(e_term)
                    self._kanji_autofilled = True
                    filled.append(f"kanji={e_term}")

        # 填“日语词条”：用命中项 kana（假名）优先
        if e_kana:
            cur = (self.add_term.text() or "").strip()
            if force or not cur or getattr(self, "_term_autofilled", True):
                if not cur or cur != e_kana:
                    self.add_term.setText(e_kana)
                    self._term_autofilled = True
                    filled.append(f"term={e_kana}")

        # 填“中文释义”
        if e_mean:
            cur = (self.add_mean.text() or "").strip()
            if force or not cur or getattr(self, "_meaning_autofilled", True):
                if not cur or cur != e_mean:
                    self.add_mean.setText(e_mean)
                    self._meaning_autofilled = True
                    filled.append("meaning")

        if filled:
            print(f"[AutoFill] 命中: {hit_key} -> {', '.join(filled)}")
        else:
            print(f"[AutoFill] 命中但未覆盖（均视为手动输入）：{hit_key}")

    # ---------- 选择单元查看词条 ----------
    def on_unit_clicked(self, item):
        # 若切换单元，先收起释义候选弹窗
        try:
            if getattr(self, "_cand_popup", None) and self._cand_popup.isVisible():
                self._cand_popup.hide()
        except Exception:
            pass

        unit = item.text()
        if unit == "所有单元":
            rows = list_cards_by_unit(self.db, None)
            self._current_unit = None
        else:
            rows = list_cards_by_unit(self.db, unit)
            self._current_unit = unit

        # 不再显示/计算总览条
        self.overview.hide()

        # 缓存与刷新
        self._current_rows_all = rows[:]
        self._shuffled = False

        # 清空搜索时临时屏蔽 textChanged，避免二次刷新
        from PyQt5 import QtCore
        with QtCore.QSignalBlocker(self.search_box):
            self.search_box.clear()
        with QtCore.QSignalBlocker(self.sort_combo):
            self.sort_combo.setCurrentIndex(0)

        # 把刷新放到事件队列，避免和上面的 UI 变更“同拍”触发重算
        QtCore.QTimer.singleShot(0, self._apply_filters_and_refresh)

        self.center_hint.setText(f"当前单元：{unit}（共 {len(rows)} 条）")

        self.unit_table.show()
        # 同步右侧“当前单元”标签
        try:
            if hasattr(self, "lbl_current_unit") and self.lbl_current_unit:
                self.lbl_current_unit.setText(unit if unit else "（请在左侧选择单元）")
        except Exception:
            pass

    def _rebuild_op_column(self):
        """
        为“操作”列（第 5 列，0 基）放置三个图标按钮：发音 / 编辑 / 删除。
        使用 setIndexWidget，按钮纯图标，无文字，更省空间。
        """
        view = self.unit_table
        model = self._card_model
        proxy = self._proxy

        rows = model.rowCount()
        for r in range(rows):
            # 获取操作列的索引
            src_idx = model.index(r, 5)
            idx = proxy.mapFromSource(src_idx)
            if not idx.isValid():
                continue

            cid = model.card_id_at(r)

            # === 新增：获取假名数据用于发音 ===
            # 第1列是假名列
            idx_term = model.index(r, 1)
            term_text = model.data(idx_term, QtCore.Qt.DisplayRole)

            cell = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(cell)
            h.setContentsMargins(4, 0, 4, 0)  # 左右边距 4
            h.setSpacing(4)  # 按钮间距调小一点，容纳三个按钮

            # 1. 喇叭按钮 (新增)
            btn_speak = QtWidgets.QToolButton(cell)
            speak_icon = self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay)
            if speak_icon.isNull():
                btn_speak.setText("🔊")
            else:
                btn_speak.setIcon(speak_icon)
            btn_speak.setAutoRaise(True)
            btn_speak.setIconSize(QtCore.QSize(16, 16))
            btn_speak.setFixedSize(24, 24)
            btn_speak.setToolTip("发音")
            # 使用 lambda 捕获当前行的文本
            btn_speak.clicked.connect(lambda _, t=term_text: self.speak_text(t))

            # 2. 编辑按钮
            btn_edit = QtWidgets.QToolButton(cell)
            btn_edit.setAutoRaise(True)
            edit_icon = QtGui.QIcon.fromTheme("document-edit")
            if edit_icon.isNull():
                btn_edit.setText("✎")
            else:
                btn_edit.setIcon(edit_icon)
            btn_edit.setIconSize(QtCore.QSize(16, 16))
            btn_edit.setFixedSize(24, 24)
            btn_edit.setToolTip("编辑")
            btn_edit.clicked.connect(lambda _, x=cid: self._edit_card_by_id(x))

            # 3. 删除按钮
            btn_del = QtWidgets.QToolButton(cell)
            btn_del.setAutoRaise(True)
            del_icon = self.style().standardIcon(QtWidgets.QStyle.SP_TrashIcon)
            if del_icon.isNull():
                btn_del.setText("🗑")
            else:
                btn_del.setIcon(del_icon)
            btn_del.setIconSize(QtCore.QSize(16, 16))
            btn_del.setFixedSize(24, 24)
            btn_del.setToolTip("删除")
            btn_del.clicked.connect(lambda _, x=cid: self._delete_card_by_id(x))

            h.addWidget(btn_speak)
            h.addWidget(btn_edit)
            h.addWidget(btn_del)
            self.unit_table.setIndexWidget(idx, cell)

    def populate_unit_table(self, rows):
        """
        M3 实现：把 rows 设置进 model，并尽量保留滚动位置、选中项与排序。
        rows 是 list(tuple)，与你现有 DB 查询返回的一致。
        """
        # 记住状态
        state = self._remember_table_state()

        # 写入模型
        self._card_model.set_rows(rows)

        # 恢复状态
        self._restore_table_state(state)
        # 显示“当前单元：xxx（共 N 条）”的信息，你已有调用处会设置，这里不重复

        # 刷新“操作”列里的按钮（每次数据重置后都重建一次）
        self._rebuild_op_column()

    def _remember_table_state(self):
        """保存：滚动条位置、当前选中 card_id、排序列/序。"""
        view = self.unit_table
        vbar = view.verticalScrollBar()
        scroll = vbar.value() if vbar else 0

        # 选中的 card_id
        sel_id = None
        sel = view.selectionModel().selectedRows() if view.selectionModel() else []
        if sel:
            # 取第一选中行的源模型行号 -> card_id
            proxy_idx = sel[0]
            src_idx = self._proxy.mapToSource(proxy_idx)
            sel_id = self._card_model.card_id_at(src_idx.row())

        # 排序状态
        header = view.horizontalHeader()
        sort_col = header.sortIndicatorSection() if header.sortIndicatorSection() >= 0 else 0
        sort_ord = header.sortIndicatorOrder() if header.sortIndicatorSection() >= 0 else QtCore.Qt.AscendingOrder

        return {"scroll": scroll, "sel_id": sel_id, "sort_col": sort_col, "sort_ord": sort_ord}

    def _restore_table_state(self, state):
        if not state:
            return
        view = self.unit_table
        header = view.horizontalHeader()
        # 不恢复历史排序，保持源顺序（我们的规则排序/打乱）
        try:
            header.setSortIndicator(-1, QtCore.Qt.AscendingOrder)  # 清除指示
        except Exception:
            pass
        # 恢复选中/滚动（保持原逻辑）
        target_row = -1
        if state.get("sel_id") is not None:
            for r in range(self._card_model.rowCount()):
                if self._card_model.card_id_at(r) == state["sel_id"]:
                    target_row = r
                    break
        if target_row >= 0:
            src_idx = self._card_model.index(target_row, 0)
            proxy_idx = self._proxy.mapFromSource(src_idx)
            view.selectRow(proxy_idx.row())
        vbar = view.verticalScrollBar()
        if vbar:
            vbar.setValue(state["scroll"])

    # 替换位置：class MainWindow 方法 add_card_from_form（去掉语言分支，固定为日语）
    def add_card_from_form(self):
        # 1) 读取表单（固定为“日语”）
        language = "日语"
        term = (self.add_term.text() or "").strip()
        meaning = (self.add_mean.text() or "").strip()
        jp_kanji = (self.add_kanji.text() or "").strip() or None
        jp_kana = (self.add_kana.text() or "").strip() or None

        # 2) 兜底：若词条为纯假名且读音未填，则自动带入罗马音
        if (not jp_kana) and _has_kana(term) and not _has_kanji(term):
            try:
                jp_kana = kana_to_romaji(term)
            except Exception:
                pass

        # 3) 校验与补全显示字段
        if not (term or jp_kanji or jp_kana):
            QtWidgets.QMessageBox.warning(self, "缺少内容", "请至少填写 日语词条／汉字写法／假名读音 之一。")
            return
        if not term:
            term = (jp_kanji or jp_kana or "").strip()

        # 必须选中具体单元（以左侧为准）
        it = self.unit_list.currentItem()
        unit_name = it.text().strip() if it else ""
        if not it or unit_name in ("", "所有单元"):
            try:
                self._msg_warn("未选择单元", "请先在左侧选择一个具体单元。")
            except Exception:
                QtWidgets.QMessageBox.warning(self, "未选择单元", "请先在左侧选择一个具体单元。")
            return

        # 使用左侧当前单元作为归属
        unit = unit_name
        add_card(self.db, language, unit, term, meaning, jp_kanji=jp_kanji, jp_kana=jp_kana)

        # 4) 清空与刷新
        # 4) 清空填写项，但保留单元选择与当前表格视图
        self.add_term.clear()
        self.add_mean.clear()
        self.add_kanji.clear()
        self.add_kana.clear()
        self._kana_autofilled = True

        # 更新“单元”下拉候选，但保留当前编辑文本
        self.refresh_units_to_combo()

        # 如新单元尚未出现在左侧列表，则追加（不改变当前选择）
        if unit and all(self.unit_list.item(i).text() != unit for i in range(self.unit_list.count())):
            self.unit_list.addItem(unit)

        # 刷新当前视图：按左侧当前选中的单元重载数据，但不重置搜索/排序/打乱
        cur_item = self.unit_list.currentItem()
        if cur_item:
            cur = cur_item.text()
            if cur == "所有单元":
                rows = list_cards_by_unit(self.db, None)
            else:
                rows = list_cards_by_unit(self.db, cur)

            # 不再显示/计算总览条
            self.overview.hide()

            self._current_rows_all = rows[:]  # 保持“原始行”
            self._apply_filters_and_refresh()  # 走你已有的搜索/排序/打乱
            self.center_hint.setText(f"当前单元：{cur}（共 {len(rows)} 条）")

        # 让焦点回到“假名”输入，便于继续录入
        self.add_term.setFocus()

        # 用非阻塞提示，~1s 自动消失（你项目已有 _notify）
        self._notify("已添加到题库")

    def add_and_start_unit(self):
        self.add_card_from_form()
        unit = self.add_unit_combo.currentText().strip()
        # 跳转复习窗口（保留原有功能）
        self.open_study_window(unit_filter=(unit if unit else None))

    def closeEvent(self, e: QtGui.QCloseEvent):
        try:
            rh = self.unit_table.verticalHeader().defaultSectionSize()
            self.settings.set("table_row_height", int(rh))
            self.settings.set("op_col_width", int(self.unit_table.columnWidth(5)))
            self.settings.save()
        except Exception:
            pass
        super().closeEvent(e)

    def delete_card_dialog(self):
        btn = self.sender()
        cid = btn.property("card_id")
        reply = QtWidgets.QMessageBox.question(
            self, "确认删除", "确定删除该词？", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply == QtWidgets.QMessageBox.Yes:
            # 删除真实 DB 记录
            delete_card(self.db, int(cid))
            # 保持当前单元视图并刷新，以“行号”重新编号
            cur_item = self.unit_list.currentItem()
            if cur_item:
                self.on_unit_clicked(cur_item)
            else:
                # 回退：刷新左侧与表格
                self.refresh_units()

    def _edit_card_by_id(self, card_id: int):
        """给 Delegate 用的入口：用现有流程完成编辑。"""
        # 复用你现有逻辑，但不依赖 sender()
        try:
            row = get_card_by_id(self.db, int(card_id))
        except Exception:
            row = None
        if not row:
            QtWidgets.QMessageBox.warning(self, "未找到", "未能读取该词条，可能已被删除。")
            return

        dlg = EditDialog(self, self.db, row)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            try:
                language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby = dlg.get_result()
                update_card_fields_full(
                    self.db, row[0], language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby
                )
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "保存失败", f"写入数据库失败：{e}")
                return

            # 刷新当前视图（保持你原有逻辑）
            cur_item = self.unit_list.currentItem()
            cur_unit = cur_item.text() if cur_item else "所有单元"
            try:
                if cur_unit == "所有单元":
                    rows = list_cards_by_unit(self.db, None)
                else:
                    rows = list_cards_by_unit(self.db, cur_unit)
                self.populate_unit_table(rows)
                self.center_hint.setText(f"当前单元：{cur_unit}（共 {len(rows)} 条）")
            except Exception:
                self.refresh_units()
            self._notify("词条已更新。")  # 非阻塞，约 1.2s 自动消失

    def _delete_card_by_id(self, card_id: int):
        reply = QtWidgets.QMessageBox.question(
            self, "确认删除", "确定删除该词？", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        delete_card(self.db, int(card_id))
        cur_item = self.unit_list.currentItem()
        if cur_item:
            self.on_unit_clicked(cur_item)
        else:
            self.refresh_units()

    def edit_card_dialog(self):
        btn = self.sender()
        card_id = btn.property("card_id")
        try:
            row = get_card_by_id(self.db, int(card_id))
        except Exception:
            row = None
        if not row:
            QtWidgets.QMessageBox.warning(self, "未找到", "未能读取该词条，可能已被删除。")
            return

        dlg = EditDialog(self, self.db, row)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            try:
                language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby = dlg.get_result()
                update_card_fields_full(
                    self.db, row[0], language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby
                )
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "保存失败", f"写入数据库失败：{e}")
                return

            # 刷新当前视图
            cur_item = self.unit_list.currentItem()
            cur_unit = cur_item.text() if cur_item else "所有单元"
            try:
                if cur_unit == "所有单元":
                    rows = list_cards_by_unit(self.db, None)
                else:
                    rows = list_cards_by_unit(self.db, cur_unit)
                self.populate_unit_table(rows)
                self.center_hint.setText(f"当前单元：{cur_unit}（共 {len(rows)} 条）")
            except Exception:
                # 保底刷新
                self.refresh_units()

            QtWidgets.QMessageBox.information(self, "已保存", "词条已更新。")

    # ---------- 快捷键（Space 用作继续） ----------
    def on_space_pressed(self):
        if hasattr(self, 'study_win') and self.study_win.isVisible():
            # delegate
            self.study_win.try_space_continue()

    # 2) 在 class MainWindow 内新增该方法（与按钮 self.export_csv_dialog 绑定一致）
    def export_csv_dialog(self):
        # 默认导出到桌面，带时间戳文件名
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_dir = os.path.join(os.path.expanduser("~"), "Desktop")
        default_path = os.path.join(default_dir, f"vocab_export_{ts}.csv")

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出为 CSV",
            default_path,
            "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            export_all_cards_to_csv(self.db, path)
            QtWidgets.QMessageBox.information(self, "导出完成", f"已导出到：{path}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", f"错误：{e}")

    def _open_unit_overview(self):
        if not getattr(self, "_current_unit", None):
            QtWidgets.QMessageBox.information(self, "提示", "请先在左侧选中一个【单个】单元。")
            return
        rows = getattr(self, "_current_rows_all", None)
        if not rows:
            QtWidgets.QMessageBox.information(self, "提示", "该单元暂无单词。")
            return
        dlg = UnitOverviewDialog(self, self._current_unit, rows)
        dlg.exec_()

    def open_settings_dialog(self):
        """打开设置对话框，保存并立即应用。"""
        try:
            dlg = SettingsDialog(self, self.settings)
        except NameError:
            # 兼容：如果你还没加 SettingsDialog 类，会抛 NameError；见本文档第 2 节补充类定义
            QtWidgets.QMessageBox.critical(self, "缺少类", "未找到 SettingsDialog 类，请按补丁第 2 节添加。")
            return

        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            vals = dlg.values()
            for k, v in vals.items():
                self.settings.set(k, v)
            self.settings.save()
            self.apply_settings()

    def apply_settings(self):
        """把设置写回到当前 UI（行高/操作列宽/字号/词典等），并重建操作列按钮。"""
        # 表格尺寸
        try:
            self.unit_table.verticalHeader().setDefaultSectionSize(
                int(self.settings.get("table_row_height", 28))
            )
            self.unit_table.setColumnWidth(
                5, int(self.settings.get("op_col_width", 84))
            )
        except Exception:
            pass

        # 字号缩放（温和处理）——仅作用在主界面的 centralWidget，避免影响外部顶级窗口（例如 KanaQuiz）
        try:
            fs = float(self.settings.get("font_scale", 1.0) or 1.0)
            base_px = 12
            cw = self.centralWidget()
            if cw is not None:
                cw.setObjectName("vocabRoot")
                cw.setStyleSheet(
                    f"#vocabRoot, #vocabRoot * {{ font-size: {int(base_px * fs)}px; }}"
                )
        except Exception:
            pass

        # 词典路径切换：如果你的 LocalJaZhDict 支持重新加载，则尝试加载；失败不影响使用
        try:
            new_dict = (self.settings.get("dict_path", "") or "").strip()
            if new_dict and os.path.exists(new_dict):
                _LOCAL_DICT.load(new_dict)
            else:
                _LOCAL_DICT.load(None)  # 回到默认
        except Exception:
            pass

        # 操作列按钮在列宽改变后重建一次，避免被挤压
        try:
            self._rebuild_op_column()
        except Exception:
            pass

    def open_stats_dialog(self):
        dlg = StatsDialog(self, self.db, use_mpl=False)  # ← 强制安全模式（纯文本/表格），不触发 Qt5Agg
        dlg.exec_()

    def open_kana_quiz(self, _checked=False):
        # 如果窗口不存在或已销毁，则创建新实例
        if not hasattr(self, "_kana_quiz_window") or self._kana_quiz_window is None:
            # 直接使用上面刚刚插入的 KanaQuiz 类
            self._kana_quiz_window = KanaQuiz(None)
            # 窗口关闭时自动清理引用
            self._kana_quiz_window.destroyed.connect(lambda *_: setattr(self, "_kana_quiz_window", None))

        self._kana_quiz_window.show()
        self._kana_quiz_window.raise_()
        self._kana_quiz_window.activateWindow()

    def _maybe_show_kana_popup(self, txt: str):
        """
        当“假名”框里输入的是纯 ASCII（罗马音）时，解析并弹出两个候选：
        ①平假名 ②片假名；按回车或点击选择写回。
        """
        try:
            s = (txt or "").strip()
            if not s:
                if getattr(self, "_kana_popup", None):
                    self._kana_popup.hide()
                return
            # 只有“像罗马音”的时候才弹候选；已有假名/汉字就不打扰
            if _is_romaji(s):
                res = romaji_to_kana(s)
                if res:
                    hira, kata = res
                    # 如果解析成功，展示两个候选
                    self._kana_popup.show_for(self.add_term, [hira, kata])
                else:
                    self._kana_popup.hide()
            else:
                self._kana_popup.hide()
        except Exception:
            # 任何异常都不要打断输入体验
            try:
                self._kana_popup.hide()
            except Exception:
                pass

    def _apply_kana_choice(self, kana: str):
        """
        把选中的假名（平或片）写回“假名”输入框。写回后，你已有的
        '假名→罗马音' 自动填充会正常把罗马音带入右侧框。
        """
        if not kana:
            return
        # 避免触发“这是用户手改”的标记
        try:
            self.add_term.blockSignals(True)
            self.add_term.setText(kana)
            self._term_autofilled = True
        finally:
            self.add_term.blockSignals(False)

        try:
            if getattr(self, "_kana_popup", None):
                self._kana_popup.hide()
        except Exception:
            pass

        # 焦点回到“中文释义”或继续在“假名”框皆可，这里保持不变

    def _on_roma_ime_edited(self, txt: str):
        """连续罗马音 → 生成两种候选：平/片；空/非法则禁用按钮（永不抛异常）"""
        try:
            s = (txt or "").strip().lower()
            if not s:
                self._btn_kana_hira.setText("");
                self._btn_kana_kata.setText("")
                self._btn_kana_hira.setEnabled(False);
                self._btn_kana_kata.setEnabled(False)
                return
            if not _is_romaji(s):
                self._btn_kana_hira.setText("非法输入");
                self._btn_kana_kata.setText("")
                self._btn_kana_hira.setEnabled(False);
                self._btn_kana_kata.setEnabled(False)
                return

            # 关键：用宽松版包装，避免半截音节导致 None
            hira, kata = romaji_to_kana_relaxed(s)

            self._btn_kana_hira.setText(hira or "")
            self._btn_kana_kata.setText(kata or "")
            self._btn_kana_hira.setEnabled(bool(hira))
            self._btn_kana_kata.setEnabled(bool(kata))
        except Exception:
            # 出错也不让 UI 崩
            self._btn_kana_hira.setText("解析失败");
            self._btn_kana_kata.setText("")
            self._btn_kana_hira.setEnabled(False);
            self._btn_kana_kata.setEnabled(False)

    def _commit_roma_enter(self):
        if self._btn_kana_hira.isEnabled():
            txt = (self._btn_kana_hira.text() or "").strip()
            if txt:
                self._apply_kana_choice(txt)

    def _apply_kana_choice(self, kana: str):
        """
        把候选假名写入“假名”行，并在“罗马音(可选)”为空或仍处于自动态时，回填罗马音。
        同时避免与 add_term 的 QCompleter 弹窗冲突。
        """
        kana = (kana or "").strip()
        if not kana:
            return
        # 1) 先把“假名” completer 的 popup（如有）藏起来，避免视觉冲突
        try:
            c = self.add_term.completer()
            if c and c.popup():
                c.popup().hide()
        except Exception:
            pass
        # 2) 回填“假名”，期间阻断信号，避免触发 completer
        try:
            self.add_term.blockSignals(True)
            self.add_term.setText(kana)
            self._term_autofilled = True
        finally:
            self.add_term.blockSignals(False)

        # 3) 若“罗马音(可选)”为空或仍处于自动填状态，则按假名反推罗马音
        try:
            roma = kana_to_romaji(kana)  # 你工程已有此函数
            cur = (self.add_kana.text() or "").strip()
            if getattr(self, "_kana_autofilled", True) or (not cur):
                self._auto_kana_in_progress = True
                try:
                    self.add_kana.setText(roma)
                    self._last_auto_romaji = roma
                    self._kana_autofilled = True
                finally:
                    self._auto_kana_in_progress = False
        except Exception:
            pass

        # 4) 方便继续操作：把光标放回中文释义或“添加”按钮（按你的习惯也可放回“假名”）
        try:
            self.add_mean.setFocus()
        except Exception:
            pass

    def _notify(self, text: str, ms: int = 1200):
        """
        非阻塞轻提示：优先状态栏，回退到气泡提示；ms 为显示时长。
        """
        try:
            self.statusBar().showMessage(text, ms)  # QMainWindow 自带；无阻塞
        except Exception:
            # 兜底：在“添加到题库”按钮附近弹出气泡
            try:
                pos = self.btn_add.mapToGlobal(QtCore.QPoint(0, self.btn_add.height()))
                QtWidgets.QToolTip.showText(pos, text, self.btn_add, self.btn_add.rect(), ms)
            except Exception:
                pass

class KanaQuiz(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("平假名/片假名 测验")
        self.resize(600, 450)

        # 样式表
        self.setStyleSheet("""
            QWidget { font-family: "Microsoft YaHei UI", sans-serif; }
            QLineEdit { 
                font-size: 24px; padding: 8px; border: 2px solid #ccc; border-radius: 8px; 
            }
            QLineEdit:focus { border-color: #3b82f6; }
            QLineEdit[state="correct"] { background-color: #d1fae5; border-color: #10b981; }
            QLineEdit[state="wrong"] { background-color: #fee2e2; border-color: #ef4444; }
            QLabel#bigChar { font-size: 90px; font-weight: bold; color: #1f2937; }
            QLabel#result { font-size: 20px; font-weight: bold; }
        """)

        self.score = 0
        self.total = 0

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(20)
        layout.setContentsMargins(40, 40, 40, 40)

        # 顶部：模式选择
        top = QtWidgets.QHBoxLayout()
        self.btn_hira = QtWidgets.QRadioButton("平假名 -> 罗马音")
        self.btn_kata = QtWidgets.QRadioButton("片假名 -> 罗马音")
        self.btn_hira.setChecked(True)
        self.btn_hira.toggled.connect(self.reset_quiz)

        font = QtGui.QFont()
        font.setPointSize(12)
        self.btn_hira.setFont(font)
        self.btn_kata.setFont(font)

        top.addWidget(self.btn_hira)
        top.addWidget(self.btn_kata)
        top.addStretch()
        layout.addLayout(top)

        # 中间：显示大字
        self.lbl_char = QtWidgets.QLabel("准备")
        self.lbl_char.setObjectName("bigChar")
        self.lbl_char.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.lbl_char, 1)

        # 结果反馈
        self.lbl_result = QtWidgets.QLabel("输入罗马音并回车")
        self.lbl_result.setObjectName("result")
        self.lbl_result.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_result.setStyleSheet("color: #6b7280;")
        layout.addWidget(self.lbl_result)

        # 输入区
        input_box = QtWidgets.QVBoxLayout()
        self.ed_input = QtWidgets.QLineEdit()
        self.ed_input.setPlaceholderText("例如: ka")
        self.ed_input.setAlignment(QtCore.Qt.AlignCenter)
        self.ed_input.returnPressed.connect(self.check_answer)
        input_box.addWidget(self.ed_input)

        self.btn_next = QtWidgets.QPushButton("跳过 / 下一题")
        self.btn_next.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_next.setFixedHeight(40)
        self.btn_next.clicked.connect(self.next_question)
        input_box.addWidget(self.btn_next)

        layout.addLayout(input_box)

        # 底部：分数
        self.lbl_score = QtWidgets.QLabel("得分: 0 / 0")
        self.lbl_score.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_score.setStyleSheet("font-size: 16px; color: #374151; margin-top: 10px;")
        layout.addWidget(self.lbl_score)

        # 数据
        self.hira_map = {
            'a':'あ','i':'い','u':'う','e':'え','o':'お',
            'ka':'か','ki':'き','ku':'く','ke':'け','ko':'こ',
            'sa':'さ','shi':'し','su':'す','se':'せ','so':'そ',
            'ta':'た','chi':'ち','tsu':'つ','te':'て','to':'と',
            'na':'な','ni':'に','nu':'ぬ','ne':'ね','no':'の',
            'ha':'は','hi':'ひ','fu':'ふ','he':'へ','ho':'ほ',
            'ma':'ま','mi':'み','mu':'む','me':'め','mo':'も',
            'ya':'や','yu':'ゆ','yo':'よ',
            'ra':'ら','ri':'り','ru':'る','re':'れ','ro':'ろ',
            'wa':'わ','wo':'を','n':'ん'
        }
        self.kata_map = {
            'a':'ア','i':'イ','u':'ウ','e':'エ','o':'オ',
            'ka':'カ','ki':'キ','ku':'ク','ke':'ケ','ko':'コ',
            'sa':'サ','shi':'シ','su':'ス','se':'セ','so':'ソ',
            'ta':'タ','chi':'チ','tsu':'ツ','te':'テ','to':'ト',
            'na':'ナ','ni':'ニ','nu':'ヌ','ne':'ネ','no':'ノ',
            'ha':'ハ','hi':'ヒ','fu':'フ','he':'ヘ','ho':'ホ',
            'ma':'マ','mi':'ミ','mu':'ム','me':'メ','mo':'モ',
            'ya':'ヤ','yu':'ユ','yo':'ヨ',
            'ra':'ラ','ri':'リ','ru':'ル','re':'レ','ro':'ロ',
            'wa':'ワ','wo':'ヲ','n':'ン'
        }

        # 别名映射
        self.alias_map = {
            'hu': 'fu', 'si': 'shi', 'zi': 'ji', 'ti': 'chi', 'tu': 'tsu',
            'jya': 'ja', 'jyu': 'ju', 'jyo': 'jo', 'nn': 'n', 'c': 'ku',
            'la': 'ra', 'li': 'ri', 'lu': 'ru', 'le': 're', 'lo': 'ro',
        }

        self.current_q = None
        self.is_waiting_next = False # 防止连击（正确时）
        self.showing_error = False   # === 关键新增：是否正在显示错误 ===
        self.reset_quiz()

    def reset_quiz(self):
        self.score = 0
        self.total = 0
        self.update_score()
        self.next_question()
        self.ed_input.setFocus()

    def next_question(self):
        # === 重置所有状态 ===
        self.is_waiting_next = False
        self.showing_error = False

        self.ed_input.clear()
        self.ed_input.setProperty("state", "")
        self.ed_input.style().unpolish(self.ed_input)
        self.ed_input.style().polish(self.ed_input)

        self.lbl_result.setText("请输入罗马音")
        self.lbl_result.setStyleSheet("color: #6b7280;")
        self.btn_next.setText("跳过 / 下一题") # 恢复按钮文字

        is_hira = self.btn_hira.isChecked()
        pool = list(self.hira_map.items()) if is_hira else list(self.kata_map.items())
        self.current_q = random.choice(pool)
        self.lbl_char.setText(self.current_q[1])

    def normalize_input(self, text):
        s = text.strip().lower()
        return self.alias_map.get(s, s)

    def check_answer(self):
        if not self.current_q: return

        # 1. 如果正在等待自动跳转（答对了），忽略回车
        if self.is_waiting_next: return

        # 2. === 关键修改 ===：如果正在显示错误，按下回车直接去下一题
        if self.showing_error:
            self.next_question()
            return

        raw_user = self.ed_input.text()
        user = self.normalize_input(raw_user)
        correct_key = self.current_q[0]

        self.total += 1

        if user == correct_key:
            # === 答对逻辑（保持不变） ===
            self.score += 1
            self.lbl_result.setText(f"✅ 正确！({correct_key})")
            self.lbl_result.setStyleSheet("color: #059669; font-weight: bold;")

            self.ed_input.setProperty("state", "correct")
            self.ed_input.style().unpolish(self.ed_input)
            self.ed_input.style().polish(self.ed_input)

            self.is_waiting_next = True
            QtCore.QTimer.singleShot(1000, self.next_question)
        else:
            # === 答错逻辑（修改） ===
            self.lbl_result.setText(f"❌ 错误，应该是: {correct_key} (按回车继续)")
            self.lbl_result.setStyleSheet("color: #dc2626; font-weight: bold;")

            self.ed_input.setProperty("state", "wrong")
            self.ed_input.style().unpolish(self.ed_input)
            self.ed_input.style().polish(self.ed_input)

            # 标记为“正在显示错误”，并更改按钮文字提示
            self.showing_error = True
            self.btn_next.setText("继续 (Enter)")

            # 这里不清除文本，保留用户的错误输入供对比，
            # 下一次回车会被上面 check_answer 开头的逻辑捕获，直接跳下一题

        self.update_score()

    def update_score(self):
        self.lbl_score.setText(f"得分: {self.score} / {self.total}")

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self._settings = settings

        form = QtWidgets.QFormLayout(self)

        # 复习/队列
        self.chk_include_not_due = QtWidgets.QCheckBox()
        self.chk_include_not_due.setChecked(bool(settings.get("include_not_due", False)))
        form.addRow("包含未到期的词", self.chk_include_not_due)

        self.spin_daily = QtWidgets.QSpinBox()
        self.spin_daily.setRange(0, 5000)
        self.spin_daily.setValue(int(settings.get("daily_limit", 100)))
        self.spin_daily.setSpecialValueText("不限")
        form.addRow("每次会话上限", self.spin_daily)

        self.chk_shuffle = QtWidgets.QCheckBox()
        self.chk_shuffle.setChecked(bool(settings.get("shuffle_on_start", True)))
        form.addRow("打开复习时自动打乱", self.chk_shuffle)

        # 视图/UI
        self.spin_row_h = QtWidgets.QSpinBox();
        self.spin_row_h.setRange(20, 60)
        self.spin_row_h.setValue(int(settings.get("table_row_height", 28)))
        form.addRow("表格行高", self.spin_row_h)

        self.spin_op_w = QtWidgets.QSpinBox();
        self.spin_op_w.setRange(60, 200)
        self.spin_op_w.setValue(int(settings.get("op_col_width", 84)))
        form.addRow("操作列宽", self.spin_op_w)

        self.dspin_font = QtWidgets.QDoubleSpinBox()
        self.dspin_font.setDecimals(1);
        self.dspin_font.setRange(0.8, 1.6);
        self.dspin_font.setSingleStep(0.1)
        self.dspin_font.setValue(float(settings.get("font_scale", 1.0)))
        form.addRow("字号缩放", self.dspin_font)

        # 词典/补全
        row = QtWidgets.QHBoxLayout()
        self.ed_dict = QtWidgets.QLineEdit(settings.get("dict_path", ""))
        btn = QtWidgets.QPushButton("浏览…")

        def _pick():
            p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择本地词典 CSV", "", "CSV 文件 (*.csv);;所有文件 (*)")
            if p: self.ed_dict.setText(p)

        btn.clicked.connect(_pick)
        row.addWidget(self.ed_dict);
        row.addWidget(btn)
        w = QtWidgets.QWidget();
        w.setLayout(row)
        form.addRow("词典文件", w)

        # 记忆偏好
        self.chk_remember = QtWidgets.QCheckBox()
        self.chk_remember.setChecked(bool(settings.get("remember_zh_preference_persist", True)))
        form.addRow("记住“中文释义→候选”的偏好", self.chk_remember)

        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept);
        box.rejected.connect(self.reject)
        form.addRow(box)

    def values(self):
        return {
            "include_not_due": self.chk_include_not_due.isChecked(),
            "daily_limit": self.spin_daily.value(),
            "shuffle_on_start": self.chk_shuffle.isChecked(),
            "table_row_height": self.spin_row_h.value(),
            "op_col_width": self.spin_op_w.value(),
            "font_scale": float(self.dspin_font.value()),
            "dict_path": self.ed_dict.text().strip(),
            "remember_zh_preference_persist": self.chk_remember.isChecked()
        }


class StatsDialog(QtWidgets.QDialog):
    """
    学习统计与可视化：
      - 总览：总卡片数、单元分布 Top10、最近新增趋势
      - 复习活动：每日复习量、每日正确率
      - 记忆质量：EF 分布、重复次数分布
      - 排程：到期预报（按天）
    没有 matplotlib 时，自动降级为“纯统计文本 + 表格”。
    """

    def __init__(self, parent, db, use_mpl=True):
        super().__init__(parent)
        self.db = db
        self.use_mpl = bool(use_mpl and HAS_MPL)  # ← 新增：本对话框是否使用 matplotlib
        self.setWindowTitle("学习统计与可视化")
        self.setMinimumSize(880, 640)

        # 确保 CJK 回退链就绪（只需调用一次也没问题）
        if self.use_mpl:
            try:
                _init_mpl_cjk_fonts()
            except Exception:
                pass

        lay = QtWidgets.QVBoxLayout(self)

        # ====== 顶部概要 ======
        self.lbl_overview = QtWidgets.QLabel("统计加载中…")
        self.lbl_overview.setWordWrap(True)
        lay.addWidget(self.lbl_overview)

        # ====== 中部：选项卡 ======
        self.tabs = QtWidgets.QTabWidget()
        lay.addWidget(self.tabs, 1)

        # 总览 tab
        self.tab_overview = QtWidgets.QWidget();
        self.tabs.addTab(self.tab_overview, "总览")
        self._build_overview_tab()

        # 活动 tab
        self.tab_activity = QtWidgets.QWidget();
        self.tabs.addTab(self.tab_activity, "复习活动")
        self._build_activity_tab()

        # 质量 tab
        self.tab_quality = QtWidgets.QWidget();
        self.tabs.addTab(self.tab_quality, "记忆质量")
        self._build_quality_tab()

        # 排程 tab
        # self.tab_due = QtWidgets.QWidget(); self.tabs.addTab(self.tab_due, "排程")
        # self._build_due_tab()

        # 加载数据（立即）
        self._load_data()
        # 延后到窗口显示后再渲染，避免 Qt5Agg 在未可见状态下 draw() 的偶发崩溃
        self._plotted = False

    # ---------- UI 构造（每个 tab） ----------
    def _build_overview_tab(self):
        l = QtWidgets.QVBoxLayout(self.tab_overview)
        if self.use_mpl:
            self.fig_ov_unit = self._make_canvas(l, "各单元词数 Top10（柱状）")
            self.fig_ov_new = self._make_canvas(l, "最近新增词条（按天，近60天）")
        else:
            # Top10：各单元词数
            self.tbl_units = QtWidgets.QTableWidget(0, 2)
            self.tbl_units.setHorizontalHeaderLabels(["单元", "词数"])
            self.tbl_units.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
            self.tbl_units.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_units.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.tbl_units.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.tbl_units.setFocusPolicy(QtCore.Qt.NoFocus)

            l.addWidget(QtWidgets.QLabel("各单元词数 Top10"))
            l.addWidget(self.tbl_units)

            # 最近新增（近60天）
            self.tbl_new = QtWidgets.QTableWidget(0, 2)
            self.tbl_new.setHorizontalHeaderLabels(["日期", "新增"])
            self.tbl_new.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_new.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_new.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.tbl_new.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.tbl_new.setFocusPolicy(QtCore.Qt.NoFocus)

            l.addWidget(QtWidgets.QLabel("最近新增（近60天）"))
            l.addWidget(self.tbl_new)

    def _build_activity_tab(self):
        l = QtWidgets.QVBoxLayout(self.tab_activity)
        if self.use_mpl:
            self.fig_act_cnt = self._make_canvas(l, "每日复习量（近90天）")
            self.fig_act_acc = self._make_canvas(l, "每日正确率（近90天）")
        else:
            self.tbl_act = QtWidgets.QTableWidget(0, 3)
            self.tbl_act.setHorizontalHeaderLabels(["日期", "复习数", "正确率%"])
            self.tbl_act.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
            for c in (1, 2): self.tbl_act.horizontalHeader().setSectionResizeMode(c,
                                                                                  QtWidgets.QHeaderView.ResizeToContents)
            self.tbl_act.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.tbl_act.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.tbl_act.setFocusPolicy(QtCore.Qt.NoFocus)

            l.addWidget(QtWidgets.QLabel("每日复习（近90天）"))
            l.addWidget(self.tbl_act)

    def _build_quality_tab(self):
        l = QtWidgets.QVBoxLayout(self.tab_quality)
        if self.use_mpl:
            self.fig_ef = self._make_canvas(l, "EF（易度）分布（直方图）")
            self.fig_rep = self._make_canvas(l, "重复次数分布（直方图）")
        else:
            self.tbl_ef = QtWidgets.QTableWidget(0, 2)
            self.tbl_ef.setHorizontalHeaderLabels(["EF 区间", "数量"])
            self.tbl_rep = QtWidgets.QTableWidget(0, 2)
            self.tbl_rep.setHorizontalHeaderLabels(["重复次数", "数量"])
            self.tbl_ef.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.tbl_ef.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.tbl_ef.setFocusPolicy(QtCore.Qt.NoFocus)

            self.tbl_rep.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.tbl_rep.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.tbl_rep.setFocusPolicy(QtCore.Qt.NoFocus)

            l.addWidget(QtWidgets.QLabel("EF 分布"))
            l.addWidget(self.tbl_ef)
            self.tbl_ef.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.tbl_ef.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.tbl_ef.setFocusPolicy(QtCore.Qt.NoFocus)

            self.tbl_rep.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            self.tbl_rep.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            self.tbl_rep.setFocusPolicy(QtCore.Qt.NoFocus)

            l.addWidget(QtWidgets.QLabel("重复次数分布"))
            l.addWidget(self.tbl_rep)

    def _make_canvas(self, parent_layout, title: str):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w);
        v.setContentsMargins(0, 0, 0, 0)
        lb = QtWidgets.QLabel(title);
        v.addWidget(lb)
        fig = plt.figure()
        canvas = FigureCanvas(fig)
        v.addWidget(canvas, 1)
        parent_layout.addWidget(w, 1)
        return canvas

    def _get_cjk_fontprop(self):
        """
        返回带多字体回退链的 FontProperties。
        Matplotlib 支持 family 为 list；这样 Yu Gothic 缺的字就回退到 YaHei/Noto/SimHei。
        """
        try:
            import matplotlib
            from matplotlib import font_manager as fm
            fams = matplotlib.rcParams.get("font.sans-serif", [])
            if fams:
                return fm.FontProperties(family=list(fams))
        except Exception:
            pass
        return None

    def _apply_axis_cjk(self, ax, fp):
        """把 CJK 字体应用到坐标轴的标题/刻度/标签。"""
        if not fp:
            return
        # 标题/轴标签
        try:
            ax.title.set_fontproperties(fp)
            ax.xaxis.label.set_fontproperties(fp)
            ax.yaxis.label.set_fontproperties(fp)
        except Exception:
            pass
        # 刻度
        try:
            for t in ax.get_xticklabels() + ax.get_yticklabels():
                t.set_fontproperties(fp)
        except Exception:
            pass
        # 图例
        try:
            leg = ax.get_legend()
            if leg:
                for t in leg.get_texts():
                    t.set_fontproperties(fp)
        except Exception:
            pass

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_plotted", False):
            try:
                self._render_all()
            finally:
                self._plotted = True

    # ---------- 数据查询 ----------
    def _load_data(self):
        self.total_cards = 0
        self.per_unit = []  # [(unit, cnt)]
        self.new_per_day = []  # [(day, cnt)]
        self.reviews_per_day = []  # [(day, n, correct)]
        self.ef_list = []  # [ef...]
        self.rep_list = []  # [rep...]
        self.due_forecast = []  # [(day, cnt)]

        cur = self.db.cursor()

        # 1) 总卡片数
        row = cur.execute("SELECT COUNT(*) FROM cards").fetchone()
        self.total_cards = int(row[0]) if row and row[0] is not None else 0

        # 2) 单元分布
        self.per_unit = cur.execute(
            "SELECT unit, COUNT(*) AS c FROM cards GROUP BY unit ORDER BY c DESC LIMIT 10"
        ).fetchall()

        # 3) 最近新增（近 60 天）
        self.new_per_day = cur.execute(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) "
            "FROM cards WHERE created_at IS NOT NULL "
            "GROUP BY d ORDER BY d DESC LIMIT 60"
        ).fetchall()
        self.new_per_day = list(reversed(self.new_per_day))  # 升序画线

        # 4) 复习活动（近 90 天）
        self.reviews_per_day = cur.execute(
            "SELECT d, COUNT(*) AS n, "
            "SUM(CASE WHEN quality>=4 THEN 1 ELSE 0 END) AS cor "
            "FROM ("
            "  SELECT substr(ts,1,10) AS d, quality "
            "  FROM reviews "
            "  WHERE substr(ts,1,10) >= date('now','-90 day')"
            ") "
            "GROUP BY d ORDER BY d DESC"
        ).fetchall()
        self.reviews_per_day = list(reversed(self.reviews_per_day))

        self.reviews_per_day = list(reversed(self.reviews_per_day))

        # 5) EF & repetition
        self.ef_list = [float(x[0]) for x in cur.execute(
            "SELECT ef FROM cards WHERE ef IS NOT NULL"
        ).fetchall() if x[0] is not None]
        self.rep_list = [int(x[0]) for x in cur.execute(
            "SELECT repetition FROM cards WHERE repetition IS NOT NULL"
        ).fetchall() if x[0] is not None]

        # 顶部概要文本
        units_cnt = len(self.per_unit)
        rev_days = len(self.reviews_per_day)
        txt = f"总词条：{self.total_cards}；单元数（TOP10）：{units_cnt}；"
        if rev_days:
            total_rev = sum(int(n) for _, n, _ in self.reviews_per_day)
            total_cor = sum(int(c) for _, _, c in self.reviews_per_day)
            acc = (100.0 * total_cor / total_rev) if total_rev else 0.0
            txt += f"近{rev_days}天复习：{total_rev} 次（正确率 {acc:.1f}%）"
        else:
            txt += "近 90 天无复习记录。"
        self.lbl_overview.setText(txt)

    # ---------- 绘图 / 填充 ----------
    def _render_all(self):
        if self.use_mpl:
            self._plot_overview()
            self._plot_activity()
            self._plot_quality()
        else:
            self._fill_tables()

    def _plot_overview(self):
        # 单元 TOP10 柱状
        ax = self.fig_ov_unit.figure.subplots()
        ax.clear()
        labels = [u or "(未分组)" for u, _ in self.per_unit]
        vals = [int(c) for _, c in self.per_unit]
        ax.bar(range(len(vals)), vals)
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels(labels, rotation=30, ha="right")
        ax.set_ylabel("词数")
        ax.set_title("各单元词数 Top10")
        fp = self._get_cjk_fontprop()
        self._apply_axis_cjk(ax, fp)
        self.fig_ov_unit.draw()

        # 最近新增折线
        ax2 = self.fig_ov_new.figure.subplots()
        ax2.clear()
        if self.new_per_day:
            ds = [d for d, _ in self.new_per_day]
            vs = [int(n) for _, n in self.new_per_day]
            ax2.plot(range(len(vs)), vs)
            ax2.set_xticks(range(0, len(ds), max(1, len(ds) // 6)))
            ax2.set_xticklabels([ds[i] for i in range(0, len(ds), max(1, len(ds) // 6))], rotation=30, ha="right")
            ax2.set_ylabel("新增")
        ax2.set_title("最近新增（近60天）")
        self._apply_axis_cjk(ax2, fp)
        self.fig_ov_new.draw()

    def _plot_activity(self):
        # 每日复习量
        ax = self.fig_act_cnt.figure.subplots()
        ax.clear()
        if self.reviews_per_day:
            ds = [d for d, _, _ in self.reviews_per_day]
            ns = [int(n) for _, n, _ in self.reviews_per_day]
            ax.plot(range(len(ns)), ns)
            ax.set_xticks(range(0, len(ds), max(1, len(ds) // 6)))
            ax.set_xticklabels([ds[i] for i in range(0, len(ds), max(1, len(ds) // 6))], rotation=30, ha="right")
            ax.set_ylabel("复习次数")
        ax.set_title("每日复习量（近90天）")
        self._apply_axis_cjk(ax, fp)
        self.fig_act_cnt.draw()

        # 每日正确率
        ax2 = self.fig_act_acc.figure.subplots()
        ax2.clear()
        if self.reviews_per_day:
            ds = [d for d, _, _ in self.reviews_per_day]
            cor = [int(c) for *_, c in self.reviews_per_day]
            tot = [int(n) for _, n, _ in self.reviews_per_day]
            acc = [(100.0 * c / n if n else 0.0) for c, n in zip(cor, tot)]
            ax2.plot(range(len(acc)), acc)
            ax2.set_ylim(0, 100)
            ax2.set_xticks(range(0, len(ds), max(1, len(ds) // 6)))
            ax2.set_xticklabels([ds[i] for i in range(0, len(ds), max(1, len(ds) // 6))], rotation=30, ha="right")
            ax2.set_ylabel("正确率%")
        ax2.set_title("每日正确率（近90天）")
        self._apply_axis_cjk(ax2, fp)
        self.fig_act_acc.draw()

    def _plot_quality(self):
        # EF 直方图
        ax = self.fig_ef.figure.subplots()
        ax.clear()
        if self.ef_list:
            ax.hist(self.ef_list, bins=10)
            ax.set_xlabel("EF")
            self._apply_axis_cjk(ax, fp)
            ax.set_ylabel("数量")
            self._apply_axis_cjk(ax, fp)
        ax.set_title("EF（易度）分布")
        self._apply_axis_cjk(ax, fp)

        self.fig_ef.draw()

        # 重复次数直方图
        ax2 = self.fig_rep.figure.subplots()
        ax2.clear()
        if self.rep_list:
            ax2.hist(self.rep_list, bins=10)
            ax2.set_xlabel("重复次数")
            self._apply_axis_cjk(ax2, fp)
            ax2.set_ylabel("数量")
            self._apply_axis_cjk(ax2, fp)
        ax2.set_title("重复次数分布")
        self._apply_axis_cjk(ax2, fp)
        self.fig_rep.draw()

    def _fill_tables(self):
        # 单元 Top10
        if hasattr(self, "tbl_units"):
            self.tbl_units.setRowCount(len(self.per_unit))
            for i, (u, c) in enumerate(self.per_unit):
                self.tbl_units.setItem(i, 0, QtWidgets.QTableWidgetItem(u or "(未分组)"))
                self.tbl_units.setItem(i, 1, QtWidgets.QTableWidgetItem(str(int(c))))
        # 新增（近60天）
        if hasattr(self, "tbl_new"):
            self.tbl_new.setRowCount(len(self.new_per_day))
            for i, (d, n) in enumerate(self.new_per_day):
                self.tbl_new.setItem(i, 0, QtWidgets.QTableWidgetItem(d))
                self.tbl_new.setItem(i, 1, QtWidgets.QTableWidgetItem(str(int(n))))
        # 活动（近90天）
        if hasattr(self, "tbl_act"):
            self.tbl_act.setRowCount(len(self.reviews_per_day))
            for i, (d, n, c) in enumerate(self.reviews_per_day):
                acc = (100.0 * int(c) / int(n) if int(n) else 0.0)
                self.tbl_act.setItem(i, 0, QtWidgets.QTableWidgetItem(d))
                self.tbl_act.setItem(i, 1, QtWidgets.QTableWidgetItem(str(int(n))))
                self.tbl_act.setItem(i, 2, QtWidgets.QTableWidgetItem(f"{acc:.1f}"))
        # EF & rep 分布（简表）
        if hasattr(self, "tbl_ef") and self.ef_list:
            # 简单以 0.2 为步长
            import math
            lo, hi = math.floor(min(self.ef_list) * 10) / 10.0, math.ceil(max(self.ef_list) * 10) / 10.0
            bins = []
            x = lo
            while x < hi + 1e-9:
                bins.append((x, x + 0.2))
                x += 0.2
            counts = [0] * len(bins)
            for v in self.ef_list:
                for i, (a, b) in enumerate(bins):
                    if (v >= a and (v < b or (i == len(bins) - 1 and v <= b))):
                        counts[i] += 1;
                        break
            self.tbl_ef.setRowCount(len(bins))
            for i, (a, b) in enumerate(bins):
                self.tbl_ef.setItem(i, 0, QtWidgets.QTableWidgetItem(f"{a:.1f}–{b:.1f}"))
                self.tbl_ef.setItem(i, 1, QtWidgets.QTableWidgetItem(str(counts[i])))
        if hasattr(self, "tbl_rep") and self.rep_list:
            from collections import Counter
            cc = Counter(self.rep_list)
            ks = sorted(cc.keys())
            self.tbl_rep.setRowCount(len(ks))
            for i, k in enumerate(ks):
                self.tbl_rep.setItem(i, 0, QtWidgets.QTableWidgetItem(str(int(k))))
                self.tbl_rep.setItem(i, 1, QtWidgets.QTableWidgetItem(str(int(cc[k]))))


class UnitOverviewDialog(QtWidgets.QDialog):
    def __init__(self, parent, unit_name: str, rows: list):
        super().__init__(parent)
        self.setWindowTitle(f"总览 - {unit_name}（{len(rows)} 条）")
        self.resize(900, 560)

        layout = QtWidgets.QVBoxLayout(self)

        tip = QtWidgets.QLabel("双列展示：假名 / 汉字写法；并列出罗马音与释义。可直接滚动背诵。")
        tip.setObjectName("muted")
        layout.addWidget(tip)

        table = QtWidgets.QTableWidget(self)
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["序号", "假名", "汉字写法", "罗马音", "释义", "重复次数"])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)

        table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QTableWidget.SelectRows)
        table.setAlternatingRowColors(True)

        table.setRowCount(len(rows))
        # 约定索引：0:id, 3:term(假名), 4:释义, 11:jp_kanji, 12:jp_kana(罗马音源), 8:repetition
        for i, r in enumerate(rows):
            # 1) 序号列（1..N）
            seq = QtWidgets.QTableWidgetItem(str(i + 1))
            seq.setTextAlignment(QtCore.Qt.AlignCenter)
            # 禁止编辑（可选）
            seq.setFlags(seq.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(i, 0, seq)

            # 2) 假名
            term_kana = (r[3] or "").strip() if len(r) > 3 and r[3] else ""
            table.setItem(i, 1, QtWidgets.QTableWidgetItem(term_kana))

            # 3) 汉字写法
            jp_kanji = (r[11] or "").strip() if len(r) > 11 and r[11] else ""
            table.setItem(i, 2, QtWidgets.QTableWidgetItem(jp_kanji))

            # 4) 罗马音（由 jp_kana 转换；你的工程里 jp_kana 存罗马音源）
            jp_kana = (r[12] or "").strip() if len(r) > 12 and r[12] else ""
            try:
                romaji = kana_to_romaji(jp_kana) if jp_kana else ""
            except Exception:
                romaji = ""
            table.setItem(i, 3, QtWidgets.QTableWidgetItem(romaji))

            # 5) 释义
            mean = (r[4] or "").strip() if len(r) > 4 and r[4] else ""
            table.setItem(i, 4, QtWidgets.QTableWidgetItem(mean))

            # 6) 重复次数（居中）
            rep = ""
            try:
                rep = str(int(r[8] or 0)) if len(r) > 8 else "0"
            except Exception:
                rep = "0"
            rep_item = QtWidgets.QTableWidgetItem(rep)
            rep_item.setTextAlignment(QtCore.Qt.AlignCenter)
            rep_item.setFlags(rep_item.flags() & ~QtCore.Qt.ItemIsEditable)
            table.setItem(i, 5, rep_item)

        layout.addWidget(table)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch()
        close_btn = QtWidgets.QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        layout.addLayout(btns)


class EditDialog(QtWidgets.QDialog):
    def __init__(self, parent, conn, row):
        super().__init__(parent)
        self.setWindowTitle("编辑词条")
        self.setModal(True)
        self.conn = conn
        self.row = row  # 完整行
        self.resize(420, 0)

        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)

        # 3) EditDialog：取消语言下拉，固定为“日语”
        # 在 EditDialog.__init__ 里，删除原有 self.cb_lang 相关代码，替换为只展示标签
        lbl_lang = QtWidgets.QLabel("日语")
        form.addRow("语言", lbl_lang)

        # 单元（下拉选择，不手动输入）
        self.cb_unit = QtWidgets.QComboBox()
        self.cb_unit.setEditable(False)
        units = list_units(conn)
        self.cb_unit.addItem("")  # 允许空单元
        for u in units:
            self.cb_unit.addItem(u)
        cur_unit = (row[2] or "").strip()
        if self.cb_unit.findText(cur_unit) < 0 and cur_unit:
            self.cb_unit.addItem(cur_unit)
        self.cb_unit.setCurrentText(cur_unit)

        # 新建单元按钮
        unit_row = QtWidgets.QHBoxLayout()
        unit_row.addWidget(self.cb_unit, 1)
        btn_new_unit = QtWidgets.QToolButton()
        btn_new_unit.setText("新建")
        btn_new_unit.clicked.connect(self._create_unit)
        unit_row.addWidget(btn_new_unit)
        unit_w = QtWidgets.QWidget()
        unit_w.setLayout(unit_row)
        form.addRow("单元", unit_w)

        # 日语词条（原文）
        self.ed_term = QtWidgets.QLineEdit(row[3] or "")
        form.addRow("日语词条", self.ed_term)

        # 汉字写法 / 假名读音
        jp_kanji = row[11] if len(row) > 11 else ""
        jp_kana = row[12] if len(row) > 12 else ""
        self.ed_kanji = QtWidgets.QLineEdit(jp_kanji or "")
        self.ed_kana = QtWidgets.QLineEdit(jp_kana or "")
        form.addRow("汉字写法", self.ed_kanji)
        form.addRow("罗马音", self.ed_kana)

        # 中文释义
        self.ed_mean = QtWidgets.QLineEdit(row[4] or "")
        form.addRow("中文释义", self.ed_mean)

        layout.addLayout(form)

        # 按钮区
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        for cb in (getattr(self, "cb_unit", None),):
            if cb:
                cb.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)

    def _create_unit(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "新建单元", "单元名：")
        name = (text or "").strip()
        if ok and name:
            if self.cb_unit.findText(name) < 0:
                self.cb_unit.addItem(name)
            self.cb_unit.setCurrentText(name)

    def _on_save(self):
        # 简单校验：日语词条或（汉字/假名）至少一项
        term = (self.ed_term.text() or "").strip()
        kanji = (self.ed_kanji.text() or "").strip()
        kana = (self.ed_kana.text() or "").strip()
        if not term and not kanji and not kana:
            QtWidgets.QMessageBox.warning(self, "校验失败", "请至少填写“日语词条”或“汉字写法/假名读音”中的一项。")
            return
        self.accept()

    # 4) EditDialog：统一返回固定语言（确保 MainWindow.edit_card_dialog 中 get_result 正常）
    # 若类中尚无 get_result，则新增；若已存在，则替换为以下实现
    def get_result(self):
        language = "日语"
        unit = (self.cb_unit.currentText() or "").strip()
        term = (self.ed_term.text() or "").strip()
        meaning = (self.ed_mean.text() or "").strip()
        jp_kanji = (self.ed_kanji.text() or "").strip() or None
        jp_kana = (self.ed_kana.text() or "").strip() or None
        # 兼容可选的 jp_ruby
        jp_ruby = None
        return language, unit, term, meaning, jp_kanji, jp_kana, jp_ruby


class StudyWindow(QtWidgets.QWidget):
    def __init__(self, db, unit_filter=None, include_all=False):
        super().__init__()
        self.db = db
        self.unit_filter = unit_filter
        self.include_all = bool(include_all)

        if unit_filter is None or unit_filter == "" or unit_filter == "所有单元":
            title_units = "全部单元"
        elif isinstance(unit_filter, (list, tuple, set)):
            title_units = f"{len(unit_filter)} 个单元"
        else:
            title_units = str(unit_filter)
        self.setWindowTitle(f"复习 - {title_units}")

        # 顶部信息栏
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        top = QtWidgets.QHBoxLayout()
        self.info_label = QtWidgets.QLabel("准备复习...")
        self.info_label.setObjectName("sectionTitle")
        top.addWidget(self.info_label)
        top.addStretch()
        v.addLayout(top)

        # 卡片展示
        card = QtWidgets.QFrame()
        card.setObjectName("panel")
        apply_shadow(card, radius=16, offset=(0, 5))
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(18, 24, 18, 18)
        card_layout.setSpacing(10)
        self.card_layout = card_layout  # 存储以便后续插入控件

        self.term_label = QtWidgets.QLabel("——")
        self.term_label.setObjectName("bigterm")
        self.term_label.setAlignment(QtCore.Qt.AlignCenter)
        card_layout.addWidget(self.term_label)

        self.mean_label = QtWidgets.QLabel("")
        self.mean_label.setAlignment(QtCore.Qt.AlignCenter)
        self.mean_label.setWordWrap(True)
        self.mean_label.setStyleSheet("color:#4b5563;")  # muted
        self.mean_label.hide()
        card_layout.addWidget(self.mean_label)

        self.btn_toggle = QtWidgets.QPushButton("显示 / 隐藏 释义")
        self.btn_toggle.setObjectName("secondary")
        self.btn_toggle.clicked.connect(self.toggle_meaning)
        self.btn_toggle.setAutoDefault(False)
        self.btn_toggle.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.btn_toggle.setFixedWidth(160)
        card_layout.addWidget(self.btn_toggle, 0, QtCore.Qt.AlignHCenter)

        # 输入与确认（仅中→日用，懒创建）
        self.answer_box = None
        self.input_answer = None
        self.btn_confirm = None

        # 评分按钮（仅日→中显示）
        rating = QtWidgets.QHBoxLayout()
        rating.setSpacing(10)
        self.btn_again = QtWidgets.QPushButton("再来一次")
        self.btn_again.setObjectName("again")
        self.btn_hard = QtWidgets.QPushButton("困难")
        self.btn_hard.setObjectName("hard")
        self.btn_good = QtWidgets.QPushButton("记住了")
        self.btn_good.setObjectName("good")
        self.btn_easy = QtWidgets.QPushButton("非常容易")
        self.btn_easy.setObjectName("easy")
        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setEnabled(False)
            rating.addWidget(b)

        self.btn_again.clicked.connect(lambda _=False: self.submit_rating(1))
        self.btn_hard.clicked.connect(lambda _=False: self.submit_rating(3))
        self.btn_good.clicked.connect(lambda _=False: self.submit_rating(4))
        self.btn_easy.clicked.connect(lambda _=False: self.submit_rating(5))

        # “没记住”按钮（仅中→日显示）
        self.btn_fail = QtWidgets.QPushButton("没记住")
        self.btn_fail.setObjectName("again")
        self.btn_fail.setEnabled(False)
        self.btn_fail.clicked.connect(self.mark_fail)
        rating.addWidget(self.btn_fail)

        card_layout.addLayout(rating)
        v.addWidget(card, 1)

        # 显著的继续按钮
        bottom = QtWidgets.QHBoxLayout()
        self.continue_btn = QtWidgets.QPushButton("继续（Space）")
        self.continue_btn.setObjectName("primary")
        self.continue_btn.setFixedHeight(50)
        self.continue_btn.setEnabled(False)
        self.continue_btn.clicked.connect(self.next_card)
        bottom.addStretch()
        bottom.addWidget(self.continue_btn, 0)
        bottom.addStretch()
        v.addLayout(bottom)

        # 内部状态
        self.queue = []
        self.idx = 0
        self.current = None
        self.session_total = 0
        self.session_correct = 0
        self.showing_mean = False
        self._rated = False
        self._ts_start = 0.0

        # === 修复：初始化 TTS 引擎 (必须加在这里) ===
        self.speech = QtTextToSpeech.QTextToSpeech()
        found_ja = False
        for locale in self.speech.availableLocales():
            if locale.name().startswith("ja"):
                self.speech.setLocale(locale)
                found_ja = True
                break
        if not found_ja:
            print("[StudyWindow] Warning: No Japanese TTS voice found. Using default.")

        # 准备复习队列 (保持在最后)
        self.prepare_queue()
        if not self.queue:
            QtWidgets.QMessageBox.information(
                self, "信息",
                "当前没有到期的单词可复习（或刚录入的单元未到首次复习时间）。"
            )
            self.close()
            return
        # 这里不要再调用 self.show_card(...)，prepare_queue 里已经调用过了

    def _get_conn(self):
        """优先取自身的 db；否则取父窗口的 db。"""
        if hasattr(self, "db") and self.db:
            return self.db
        p = self.parent()
        return getattr(p, "db", None) if p else None

    def _record_review(self, quality: int, elapsed_ms: Optional[int] = None):
        """写 reviews 事件（失败不抛错，保证复习流程不断）"""
        try:
            conn = self._get_conn()
            if not conn or not self.current:
                return
            row, mode = self.current
            card_id = int(row[0]) if row and row[0] is not None else None
            if not card_id:
                return
            insert_review(conn, card_id, int(mode), int(quality), elapsed_ms)
        except Exception:
            # 不阻断主流程
            pass

    def submit_rating(self, quality: int):
        # 防重复评分
        if self._rated:
            return

        # ……（此处保持你原有的打分 → 更新间隔/重复/ef/到期 的逻辑）……
        # 例：update_card_after_rating(self.db, row, quality) 之类，不改你现有实现

        # 统计
        if quality in (4, 5):  # 你原来如何统计“正确”可保留
            self.session_correct += 1

        # 记录 reviews
        elapsed = int((time.monotonic() - self._ts_start) * 1000) if self._ts_start else None
        self._record_review(quality, elapsed_ms=elapsed)

        # 只在首次评分时+1
        self.session_total += 1

        # 标记已评分 & 放行“继续”
        self._rated = True
        self.continue_btn.setEnabled(True)
        self.continue_btn.setDefault(True)
        self.continue_btn.setFocus()

        # 刷新顶部统计
        self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

    def prepare_queue(self):
        # 单元归一：None 表示所有单元
        unit = getattr(self, 'unit_filter', None)
        if unit in ("所有单元", "", None):
            unit = None

        # 调试打印，便于核对传入条件和数据量
        try:
            cur = self.db.cursor()
            total = cur.execute('SELECT COUNT(*) FROM cards').fetchone()[0]
            print(f"[Study] unit={unit!r}, include_all={getattr(self, 'include_all', False)}, total_cards={total}")
        except Exception:
            pass

        # 1) 优先按“不过滤到期”或“无到期时回退到全部”
        if getattr(self, 'include_all', False):
            rows = list_cards_by_unit(self.db, unit=unit)
        else:
            rows = list_due_cards(self.db, unit=unit)
            if not rows:  # 无到期则回退到“全部”
                rows = list_cards_by_unit(self.db, unit=unit)

        # 额外兜底：若是按某单元取不到，退回所有单元
        if not rows and unit is not None:
            rows = list_cards_by_unit(self.db, unit=None)

        # 2) 题库真的为空才提示
        self.queue = []
        n = len(rows)
        if n == 0:
            QtWidgets.QMessageBox.information(self, "提示", "题库为空，请先添加单词。")
            self.idx = -1
            return

        # 3) 组卷：日→中 70% / 中→日 30%
        num_mode0 = int(n * 1.0)  # 模式0：日→中
        num_mode1 = n - num_mode0  # 模式1：中→日
        modes = [0] * num_mode0 + [1] * num_mode1
        random.shuffle(modes)

        for r, m in zip(rows, modes):
            self.queue.append((r, m))

        # 4) 开始第一题
        self.idx = 0
        self.show_card(self.queue[self.idx])

    def _ensure_answer_box(self):
        if self.answer_box:
            return
        self.answer_box = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(self.answer_box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.input_answer = QtWidgets.QLineEdit()
        self.input_answer.setPlaceholderText("输入假名或汉字")
        self.btn_confirm = QtWidgets.QPushButton("确认")
        self.btn_confirm.setObjectName("secondary")
        self.btn_confirm.clicked.connect(self.check_answer)
        h.addWidget(self.input_answer, 1)
        h.addWidget(self.btn_confirm, 0)
        # 插入到“显示/隐藏 释义”按钮之后
        idx = self.card_layout.indexOf(self.btn_toggle)
        self.card_layout.insertWidget(idx + 1, self.answer_box)

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        key = e.key()
        # Space：先展示释义；已评分后才进入下一条
        if key == QtCore.Qt.Key_Space:
            if not getattr(self, "showing_mean", False):
                self.toggle_meaning()
            else:
                # 仅在已评分（或当前是中→日模式）时允许继续
                if getattr(self, "_rated", False) or (self.current and self.current[1] == 1):
                    self.next_card()
            return

        # 数字键：1=again(1)  2=hard(3)  3=good(4)  4=easy(5)
        if key in (QtCore.Qt.Key_1, QtCore.Qt.Key_2, QtCore.Qt.Key_3, QtCore.Qt.Key_4):
            # 已评分则忽略，防止重复计数
            if self._rated:
                return
            # 未展开先展开
            if not self.showing_mean:
                self.toggle_meaning()
                return
            # 仅“日→中”用数字评分；“中→日”走确认按钮
            if self.current and self.current[1] == 0:
                mp = {
                    QtCore.Qt.Key_1: 1,
                    QtCore.Qt.Key_2: 3,
                    QtCore.Qt.Key_3: 4,
                    QtCore.Qt.Key_4: 5,
                }
                self.submit_rating(mp[key])
            return

        # 其他按键走默认
        super().keyPressEvent(e)

    def show_card(self, card):
        row, mode = card
        self.current = (row, mode)
        self.showing_mean = False

        # 清理释义 & 禁用继续
        self.mean_label.clear()
        self.mean_label.hide()
        self.continue_btn.setEnabled(False)

        # 停止上一次发音
        self.speech.stop()

        # 评分按钮：日→中可见但初始禁用
        if self.current and self.current[1] == 0:
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(False)

        # 重置状态 & 本题计时
        self._rated = False
        self._ts_start = time.monotonic()

        # 控制评分按钮可见性
        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setVisible(mode == 0)
            b.setEnabled(False)
        self.btn_fail.setVisible(mode == 1)
        self.btn_fail.setEnabled(mode == 1)

        # 输入区域
        if mode == 1:
            self._ensure_answer_box()
            self.answer_box.show()
            self.input_answer.clear()
            self.input_answer.setEnabled(True)
            self.btn_confirm.setEnabled(True)
            self.input_answer.setFocus()
        else:
            if self.answer_box:
                self.answer_box.hide()

        # 展示题面
        if mode == 0:
            # === 模式 0: 日语 -> 中文 ===
            kana = pick_kana(row)
            kanji = get_jp_kanji(row)
            romaji = pick_romaji(row, kana)

            # === 新增：自动朗读假名 ===
            kana_text = kana or (row[3] or "").strip()
            if kana_text:
                self.speech.say(kana_text)

            big_css = "font-size:48px; font-weight:bold; line-height:1.2;"
            small_css = "font-size:18px; color:#6b7280; line-height:1.2;"
            parts = []
            if romaji:
                parts.append(f"<div style='{small_css}'>{html.escape(romaji)}</div>")
            if kana:
                parts.append(f"<div style='{big_css}'>{html.escape(kana)}</div>")
            if kanji:
                parts.append(f"<div style='{small_css}'>{html.escape(kanji)}</div>")
            if not parts:
                term = (row[3] or "").strip()
                parts.append(f"<div style='{big_css}'>{html.escape(term) if term else '——'}</div>")
            self.term_label.setText("<div style='text-align:center'>" + "".join(parts) + "</div>")
            self.term_label.show()
        else:
            # === 模式 1: 中文 -> 日语 ===
            zh = (row[4] or "").strip()
            self.term_label.setText(
                f"<div style='text-align:center; font-size:32px; font-weight:bold'>{html.escape(zh) if zh else '——'}</div>")
            self.term_label.show()

    def _show_jp_answer_block(self, row, correct: bool):
        # 统一答案展示块（含对错提示）
        kana = pick_kana(row)
        kanji = get_jp_kanji(row)
        if not kana and not kanji:
            # 回退 term
            term = (row[3] or "").strip()
            kana = term if term else ""
        romaji = pick_romaji(row, kana)
        big_css = "font-size:28px; font-weight:800; line-height:1.2; font-family:'Zen Maru Gothic','Noto Sans CJK JP';"
        small_css = "font-size:16px; color:#6b7280; line-height:1.2; font-family:'Zen Maru Gothic','Noto Sans CJK JP';"
        state_css = "color:#059669;" if correct else "color:#b91c1c;"
        state_txt = "✔ 正确" if correct else "✘ 错误"
        answer_parts = [f"<div style='{state_css}'>{state_txt}</div>"]
        if romaji:
            answer_parts.append(f"<div style='{small_css}'>{html.escape(romaji)}</div>")
        if kana:
            answer_parts.append(f"<div style='{big_css}'>{html.escape(kana)}</div>")
        if kanji:
            answer_parts.append(f"<div style='{small_css}'>{html.escape(kanji)}</div>")
        zh = (row[4] or "").strip()
        tip = "<div style='text-align:center'>" + "".join(answer_parts) + (
            f"<div style='{small_css}'>{html.escape(zh)}</div>" if zh else "") + "</div>"
        return tip

    def _build_jp_neutral_block(self, row):
        # 仅展示 romaji/kana/kanji 与中文释义，不显示“正确/错误”提示
        kana = pick_kana(row)
        kanji = get_jp_kanji(row)
        if not kana and not kanji:
            term = (row[3] or "").strip()
            kana = term if term else ""
        romaji = pick_romaji(row, kana)
        big_css = "font-size:28px; font-weight:normal; line-height:1.2; font-family:'Zen Maru Gothic','Noto Sans CJK JP';"
        small_css = "font-size:16px; color:#6b7280; line-height:1.2; font-family:'Zen Maru Gothic','Noto Sans CJK JP';"
        parts = []
        if romaji:
            parts.append(f"<div style='{small_css}'>{html.escape(romaji)}</div>")
        if kana:
            parts.append(f"<div style='{big_css}'>{html.escape(kana)}</div>")
        if kanji:
            parts.append(f"<div style='{small_css}'>{html.escape(kanji)}</div>")
        zh = (row[4] or "").strip()
        if zh:
            parts.append(f"<div style='{small_css}'>{html.escape(zh)}</div>")
        return "<div style='text-align:center'>" + "".join(parts) + "</div>"

    def check_answer(self):
        if not self.current:
            return
        row, mode = self.current
        if mode != 1:
            return

        user_input = norm(self.input_answer.text())
        answers = set()
        kana = pick_kana(row)
        kanji = get_jp_kanji(row)
        if kana: answers.add(norm(kana))
        if kanji: answers.add(norm(kanji))
        term = norm(row[3])
        if term and (_has_kana(term) or _has_kanji(term)):
            answers.add(term)

        correct = bool(user_input) and (norm(user_input) in answers)

        # === 新增：无论对错，只要展示了答案，就朗读正确的假名 ===
        kana_text = kana or term or ""
        if kana_text:
            self.speech.stop()
            self.speech.say(kana_text)

        # 只在首次判定时计数 & 正确数
        if not self._rated:
            self.session_total += 1
            if correct:
                self.session_correct += 1
            self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

        # 放行继续 & 置为“已评分”
        self.continue_btn.setEnabled(True)
        self.continue_btn.setDefault(True)
        self.continue_btn.setFocus()
        self._rated = True

        # 记录 reviews
        quality = 4 if correct else 1
        elapsed = int((time.monotonic() - self._ts_start) * 1000) if self._ts_start else None
        self._record_review(quality, elapsed_ms=elapsed)

        # 兜底判定逻辑 (本地词典反查)
        if (not correct) and _has_kanji(user_input):
            std_kana = norm(pick_kana(row) or "")
            if not std_kana:
                t = (row[3] or "").strip()
                std_kana = norm(t) if t and _has_kana(t) else ""
            if std_kana:
                try:
                    rec = _LOCAL_DICT.get_full(user_input)
                    if rec and norm(rec[1]) == std_kana:
                        correct = True
                except Exception:
                    pass

        # 展示答案
        self.mean_label.setText(self._show_jp_answer_block(row, correct))
        self.mean_label.show()
        self.showing_mean = True

        # 更新记忆算法
        q = 4 if correct else 1
        interval, repetition, ef = sm2_update(row, q)
        last_review = datetime.now().astimezone().isoformat(timespec="seconds")
        update_card_review(self.db, row[0], interval, repetition, ef, last_review, None)

        if self.answer_box:
            self.input_answer.setEnabled(False)
            self.btn_confirm.setEnabled(False)
        self.btn_fail.setEnabled(False)

    def mark_fail(self):
        if not self.current:
            return
        row, mode = self.current
        if mode != 1:
            return

        # 无需输入，直接按错误处理并展示答案
        self.mean_label.setText(self._show_jp_answer_block(row, correct=False))
        self.mean_label.show()
        self.showing_mean = True

        # 更新记忆算法与计数（质量=1）
        interval, repetition, ef = sm2_update(row, 1)
        last_review = datetime.utcnow().isoformat()
        update_card_review(self.db, row[0], interval, repetition, ef, last_review, None)
        self.session_total += 1
        self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

        # UI 状态
        self.continue_btn.setEnabled(True)
        if self.answer_box:
            self.input_answer.setEnabled(False)
            self.btn_confirm.setEnabled(False)
        self.btn_fail.setEnabled(False)

        self.continue_btn.setEnabled(True)
        self.continue_btn.setDefault(True)
        self.continue_btn.setFocus()
        self._rated = True

        # 允许继续
        self.continue_btn.setEnabled(True)
        self.continue_btn.setDefault(True)
        self.continue_btn.setFocus()
        self._rated = True

        # 记录 reviews（again=1）
        elapsed = int((time.monotonic() - self._ts_start) * 1000) if self._ts_start else None
        self._record_review(1, elapsed_ms=elapsed)

    def toggle_meaning(self):
        if not self.current:
            return
        row, mode = self.current

        # 当前已显示 -> 隐藏
        if self.showing_mean:
            self.mean_label.hide()
            self.showing_mean = False
            if mode == 0:
                # 日→中：未评分仍禁继续；评分按钮根据 _rated 决定
                for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                    b.setEnabled(not self._rated)
                self.continue_btn.setEnabled(False)
                self.continue_btn.setDefault(True)
                self.continue_btn.setFocus()
            else:
                # 中→日：允许继续输入
                if self.answer_box:
                    self.input_answer.setEnabled(True)
                    self.btn_confirm.setEnabled(True)
                self.continue_btn.setEnabled(False)
            return

        # 隐藏 -> 展开
        if mode == 0:
            # 日→中：展开显示中文释义；启用评分；继续按钮仍禁用（等待评分）
            zh = (row[4] or "").strip()
            self.mean_label.setText(zh)
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(not self._rated)
            self.continue_btn.setEnabled(False)
        else:
            # 中→日：展开显示“中立答案块”（不显示对/错，不计分）
            self.mean_label.setText(self._build_jp_neutral_block(row))
            self.continue_btn.setEnabled(True)
            if self.answer_box:
                self.input_answer.setEnabled(False)
                self.btn_confirm.setEnabled(False)
            self.btn_fail.setEnabled(False)

        self.mean_label.show()
        self.showing_mean = True

        # 把键盘焦点统一给“继续”（Space 一致）
        self.continue_btn.setDefault(True)
        self.continue_btn.setFocus()

    def next_card(self):
        self._rated = False
        self._ts_start = time.monotonic()
        self.showing_mean = False
        self.continue_btn.setEnabled(False)
        # 如有评分键可见：禁用待展开
        if self.current and self.current[1] == 0:
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(False)
        self.idx += 1
        if self.idx >= len(self.queue):
            QtWidgets.QMessageBox.information(self, "完成", "本轮复习完成。")
            self.close()
            return
        self.show_card(self.queue[self.idx])

    def try_space_continue(self):
        # 已允许继续：直接下一条
        if self.continue_btn.isEnabled():
            self.next_card()
            return
        # 未判定/未评分：先展开释义
        self.toggle_meaning()

def main():
    app = QtWidgets.QApplication(sys.argv)

    # 1. 加载字体并获取真实名称
    real_font_family = load_custom_font(app)

    # 2. 生成带真实字体名的 CSS 并应用
    style_sheet = get_app_style(real_font_family)
    app.setStyleSheet(style_sheet)

    w = MainWindow()
    w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
