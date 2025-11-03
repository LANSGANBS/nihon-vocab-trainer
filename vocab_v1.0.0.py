# vocab_trainer_units_v2.py
# 改进版：主界面先展示单元，录入直接进题库（不在录入时设置到期），学习页列出单元并按SM-2复习
# 运行: python vocab_trainer_units_v2.py
# 依赖: pip install PyQt5

import sys
import os
import sqlite3
import csv
import random
from datetime import datetime, timedelta
from PyQt5 import QtWidgets, QtCore, QtGui

DB_PATH = os.path.join(os.path.expanduser("~"), "vocab_trainer_units_v2.db")

# --------------------------
# 数据库初始化
# --------------------------
def init_db(path=DB_PATH):
    first = not os.path.exists(path)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    # ensure 'unit' exists (back-compat)
    cols = [c[1] for c in cur.execute('PRAGMA table_info(cards)').fetchall()]
    if 'unit' not in cols:
        try:
            cur.execute('ALTER TABLE cards ADD COLUMN unit TEXT DEFAULT ""')
            conn.commit()
        except Exception:
            pass
    return conn

# --------------------------
# DB 操作
# --------------------------
def add_card(conn, language, unit, term, meaning):
    now = datetime.utcnow().isoformat()
    # 不在录入时设置人为到期：我们仍写入 due_date = created_at so 程序能基于时间判定是否需要复习（内部控制）
    due = now
    cur = conn.cursor()
    cur.execute('''
    INSERT INTO cards (language, unit, term, meaning, created_at, last_review, interval, repetition, ef, due_date)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (language, unit, term, meaning, now, None, 0, 0, 2.5, due))
    conn.commit()
    return cur.lastrowid

def list_units(conn):
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT unit FROM cards ORDER BY unit')
    rows = [r[0] for r in cur.fetchall() if r[0] and r[0] != ""]
    return rows

def list_cards_by_unit(conn, unit=None):
    cur = conn.cursor()
    if unit is None or unit == "" or unit == "所有单元":
        cur.execute('SELECT * FROM cards ORDER BY id')
    else:
        cur.execute('SELECT * FROM cards WHERE unit = ? ORDER BY id', (unit,))
    return cur.fetchall()

def list_due_cards(conn, unit=None):
    cur = conn.cursor()
    if unit and unit != "" and unit != "所有单元":
        cur.execute('SELECT * FROM cards WHERE unit = ? ORDER BY id', (unit,))
    else:
        cur.execute('SELECT * FROM cards ORDER BY id')
    return cur.fetchall()


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

def import_from_csv(conn, csv_path, default_unit=""):
    added = 0
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            # 支持 language,unit,term,meaning 或 language,term,meaning
            if len(row) >= 4:
                language, unit, term, meaning = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
            else:
                language, term, meaning = row[0].strip(), row[1].strip(), (row[2].strip() if len(row) > 2 else "")
                unit = default_unit
            if term:
                add_card(conn, language or "日语", unit or "", term, meaning)
                added += 1
    return added

def export_to_csv(conn, csv_path, unit=None):
    rows = list_cards_by_unit(conn, unit)
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'language', 'unit', 'term', 'meaning', 'created_at', 'last_review', 'interval', 'repetition', 'ef', 'due_date'])
        for r in rows:
            writer.writerow(r)
    return len(rows)

# --------------------------
# SM-2 算法（相同实现）
# --------------------------
def sm2_update(card_row, quality):
    _id, language, unit, term, meaning, created_at, last_review, interval, repetition, ef, due_date = card_row
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

# --------------------------
# 工具：解析 ISO 时间
# --------------------------
def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
        except:
            return None

# --------------------------
# UI 样式和窗口
# --------------------------
APP_STYLE = """
QWidget { font-family: "Microsoft YaHei", "PingFang SC", Arial; color: #222; }
QMainWindow { background: #f4f7fb; }
QFrame#panel { background: white; border-radius:10px; padding:10px; }
QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #4b9bff, stop:1 #6ee7ff); color: white; border-radius:8px; padding:8px; font-weight:700; }
QPushButton#primary:disabled { background: #bcdfff; color:#e7f5ff; }
QPushButton#accent { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #ff9b6b, stop:1 #ffd36b); color: white; border-radius:8px; padding:8px; font-weight:600; }
QPushButton#secondary { background: white; border:1px solid #e6eef9; border-radius:8px; padding:6px; }
QLineEdit, QComboBox, QTextEdit { background: white; border:1px solid #e9f0fb; border-radius:6px; padding:6px; }
QLabel#title { font-size:18px; font-weight:800; }
QLabel#bigterm { font-size:48px; font-weight:800; color:#222; }
"""

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = init_db()
        self.setWindowTitle("词汇训练器 v2（单元优先）")
        self.resize(1100, 700)
        self.setMinimumSize(900, 600)
        self.setStyleSheet(APP_STYLE)

        # 主布局：左单元列表，中间单元单词或添加，右侧空（可扩展）
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_l = QtWidgets.QHBoxLayout(central)
        main_l.setContentsMargins(12,12,12,12)
        main_l.setSpacing(12)

        # 左：单元列表（明显）
        left_frame = QtWidgets.QFrame()
        left_frame.setObjectName("panel")
        left_frame.setMaximumWidth(260)
        left_layout = QtWidgets.QVBoxLayout(left_frame)
        left_layout.setContentsMargins(10,10,10,10)

        lbl = QtWidgets.QLabel("单元列表")
        lbl.setObjectName("title")
        left_layout.addWidget(lbl)

        self.unit_list = QtWidgets.QListWidget()
        self.unit_list.itemClicked.connect(self.on_unit_clicked)
        left_layout.addWidget(self.unit_list, 1)

        btn_new_unit = QtWidgets.QPushButton("新建单元")
        btn_new_unit.setObjectName("secondary")
        btn_new_unit.clicked.connect(self.create_unit_dialog)
        left_layout.addWidget(btn_new_unit)

        btn_refresh_units = QtWidgets.QPushButton("刷新单元")
        btn_refresh_units.setObjectName("secondary")
        btn_refresh_units.clicked.connect(self.refresh_units)
        left_layout.addWidget(btn_refresh_units)

        main_l.addWidget(left_frame)

        # 中：主要区（顶部是添加，默认显示“选择单元后查看单词”）
        center_frame = QtWidgets.QFrame()
        center_frame.setObjectName("panel")
        center_layout = QtWidgets.QVBoxLayout(center_frame)
        center_layout.setContentsMargins(14,14,14,14)

        # 添加区域（两个框：日语 + 中文释义）和单元选择
        add_row = QtWidgets.QHBoxLayout()
        form_left = QtWidgets.QFormLayout()
        self.add_language = QtWidgets.QComboBox()
        self.add_language.setEditable(True)
        self.add_language.addItems(["日语", "韩语", "其他"])
        form_left.addRow("语言", self.add_language)

        self.add_unit_combo = QtWidgets.QComboBox()
        self.add_unit_combo.setEditable(True)
        self.refresh_units_to_combo()
        form_left.addRow("单元", self.add_unit_combo)

        self.add_term = QtWidgets.QLineEdit()
        form_left.addRow("日语词条", self.add_term)
        self.add_mean = QtWidgets.QLineEdit()
        form_left.addRow("中文释义", self.add_mean)
        add_row.addLayout(form_left, 1)

        btncol = QtWidgets.QVBoxLayout()
        self.btn_add = QtWidgets.QPushButton("添加到题库")
        self.btn_add.setObjectName("secondary")
        self.btn_add.clicked.connect(self.add_card_from_form)
        btncol.addWidget(self.btn_add)
        btncol.addStretch()
        add_row.addLayout(btncol)
        center_layout.addLayout(add_row)

        center_layout.addSpacing(8)
        self.center_hint = QtWidgets.QLabel("请选择左侧单元查看该单元的词条，或选择“所有单元”")
        center_layout.addWidget(self.center_hint)

        # 单元词表（隐藏直到选单元）
        self.unit_table = QtWidgets.QTableWidget()
        self.unit_table.setColumnCount(5)
        self.unit_table.setHorizontalHeaderLabels(["ID","语言","词条","释义","操作"])
        header = self.unit_table.horizontalHeader()
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.unit_table.hide()
        center_layout.addWidget(self.unit_table, 1)

        main_l.addWidget(center_frame, 1)

        # 右：学习/复习快捷（简化）
        right_frame = QtWidgets.QFrame()
        right_frame.setObjectName("panel")
        right_layout = QtWidgets.QVBoxLayout(right_frame)
        right_layout.setContentsMargins(10,10,10,10)

        right_layout.addWidget(QtWidgets.QLabel("学习 / 复习"))
        self.study_unit_select = QtWidgets.QComboBox()
        self.study_unit_select.addItem("所有单元")
        self.refresh_study_units()
        right_layout.addWidget(self.study_unit_select)

        self.btn_go_study = QtWidgets.QPushButton("开始复习")
        self.btn_go_study.setObjectName("accent")
        self.btn_go_study.clicked.connect(self.open_study_window)
        right_layout.addWidget(self.btn_go_study)

        right_layout.addStretch()
        main_l.addWidget(right_frame, 0)

        # UI 初始化
        self.refresh_units()

        # 键盘快捷（space 作继续）
        self.shortcut_space = QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self)
        self.shortcut_space.activated.connect(self.on_space_pressed)

    # ---------- 单元管理 ----------
    def refresh_units(self):
        self.unit_list.clear()
        self.unit_list.addItem("所有单元")
        for u in list_units(self.db):
            self.unit_list.addItem(u)
        self.refresh_units_to_combo()
        self.refresh_study_units()
        self.unit_table.hide()
        self.center_hint.setText("请选择左侧单元查看该单元的词条，或选择“所有单元”")

    def refresh_units_to_combo(self):
        units = list_units(self.db)
        cur = self.add_unit_combo.currentText() if hasattr(self, 'add_unit_combo') else ""
        self.add_unit_combo.clear()
        self.add_unit_combo.addItem("")
        for u in units:
            self.add_unit_combo.addItem(u)
        # restore
        idx = self.add_unit_combo.findText(cur)
        if idx >= 0:
            self.add_unit_combo.setCurrentIndex(idx)
        else:
            self.add_unit_combo.setEditText(cur)

    def refresh_study_units(self):
        units = list_units(self.db)
        self.study_unit_select.clear()
        self.study_unit_select.addItem("所有单元")
        for u in units:
            self.study_unit_select.addItem(u)

    def create_unit_dialog(self):
        text, ok = QtWidgets.QInputDialog.getText(self, "新建单元", "单元名：")
        if ok and text.strip():
            # don't add an empty card, just ensure unit exists by creating a dummy? Better: just refresh combos (unit will appear after first card added)
            self.add_unit_combo.setEditText(text.strip())
            self.refresh_units_to_combo()

    # ---------- 选择单元查看词条 ----------
    def on_unit_clicked(self, item):
        unit = item.text()
        if unit == "所有单元":
            rows = list_cards_by_unit(self.db, None)
        else:
            rows = list_cards_by_unit(self.db, unit)
        self.populate_unit_table(rows)
        self.unit_table.show()
        self.center_hint.setText(f"当前单元：{unit}（共 {len(rows)} 条）")

    def populate_unit_table(self, rows):
        self.unit_table.setRowCount(0)
        for r in rows:
            row_idx = self.unit_table.rowCount()
            self.unit_table.insertRow(row_idx)
            id_item = QtWidgets.QTableWidgetItem(str(r[0])); id_item.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)
            lang_item = QtWidgets.QTableWidgetItem(r[1] or "")
            term_item = QtWidgets.QTableWidgetItem(r[3] or "")
            mean_item = QtWidgets.QTableWidgetItem(r[4] or "")
            due_item = QtWidgets.QTableWidgetItem(r[10] or "")
            for col, it in enumerate([id_item, lang_item, term_item, mean_item, due_item]):
                self.unit_table.setItem(row_idx, col, it)
            # actions
            self.unit_table.setItem(row_idx, 4, QtWidgets.QTableWidgetItem(""))  # 清空文字
            action_w = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(action_w)
            h.setContentsMargins(0, 0, 0, 0)
            btn_edit = QtWidgets.QPushButton("编辑")
            btn_edit.setProperty("card_id", r[0])
            btn_edit.clicked.connect(self.edit_card_dialog)
            btn_delete = QtWidgets.QPushButton("删除")
            btn_delete.setProperty("card_id", r[0])
            btn_delete.clicked.connect(self.delete_card_dialog)
            h.addWidget(btn_edit);
            h.addWidget(btn_delete)
            self.unit_table.setCellWidget(row_idx, 4, action_w)

    # ---------- 添加卡片 ----------
    def add_card_from_form(self):
        language = self.add_language.currentText().strip() or "日语"
        unit = self.add_unit_combo.currentText().strip()
        term = self.add_term.text().strip()
        meaning = self.add_mean.text().strip()
        if not term:
            QtWidgets.QMessageBox.warning(self, "输入错误", "请填写日语词条。")
            return
        add_card(self.db, language, unit, term, meaning)
        self.add_term.clear(); self.add_mean.clear()
        self.refresh_units()
        QtWidgets.QMessageBox.information(self, "已添加", "已将该单词加入题库。")

    def add_and_start_unit(self):
        self.add_card_from_form()
        unit = self.add_unit_combo.currentText().strip()
        # jump to study window for this unit
        self.open_study_window(unit_filter=(unit if unit else None))

    # ---------- 编辑 / 删除 ----------
    def edit_card_dialog(self):
        btn = self.sender()
        cid = btn.property("card_id")
        cur = self.db.cursor()
        cur.execute('SELECT * FROM cards WHERE id = ?', (cid,))
        row = cur.fetchone()
        if not row:
            return
        dlg = EditDialog(self, row)
        if dlg.exec_():
            lang, unit, term, mean = dlg.get_values()
            update_card_fields(self.db, cid, lang, unit, term, mean)
            self.refresh_units()

    def delete_card_dialog(self):
        btn = self.sender()
        cid = btn.property("card_id")
        reply = QtWidgets.QMessageBox.question(self, "确认删除", "确定删除该词？", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if reply == QtWidgets.QMessageBox.Yes:
            delete_card(self.db, cid)
            self.refresh_units()

    # ---------- 学习窗口 ----------
    def open_study_window(self, unit_filter=None):
        if unit_filter is None:
            selected = self.study_unit_select.currentText()
            unit_filter = None if selected == "所有单元" else selected
        self.study_win = StudyWindow(self.db, unit_filter)
        self.study_win.show()

    # ---------- 快捷键（Space 用作继续） ----------
    def on_space_pressed(self):
        if hasattr(self, 'study_win') and self.study_win.isVisible():
            # delegate
            self.study_win.try_space_continue()

# ---------- 编辑对话框 ----------
class EditDialog(QtWidgets.QDialog):
    def __init__(self, parent, row):
        super().__init__(parent)
        self.setWindowTitle("编辑词条")
        self.setMinimumWidth(420)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.lang = QtWidgets.QLineEdit(row[1] or "")
        self.unit = QtWidgets.QLineEdit(row[2] or "")
        self.term = QtWidgets.QLineEdit(row[3] or "")
        self.mean = QtWidgets.QLineEdit(row[4] or "")
        form.addRow("语言：", self.lang)
        form.addRow("单元：", self.unit)
        form.addRow("日语词条：", self.term)
        form.addRow("中文释义：", self.mean)
        layout.addLayout(form)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
    def get_values(self):
        return self.lang.text().strip(), self.unit.text().strip(), self.term.text().strip(), self.mean.text().strip()

class StudyWindow(QtWidgets.QWidget):
    def __init__(self, db, unit_filter=None):
        super().__init__()
        self.db = db
        self.unit_filter = unit_filter
        self.setWindowTitle(f"复习 - {'全部单元' if not unit_filter else unit_filter}")
        self.resize(760, 520)
        self.setStyleSheet(APP_STYLE)

        # 顶部信息栏
        v = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.info_label = QtWidgets.QLabel("准备复习...")
        top.addWidget(self.info_label)
        top.addStretch()
        v.addLayout(top)

        # 卡片展示
        card = QtWidgets.QFrame()
        card.setObjectName("panel")
        card_layout = QtWidgets.QVBoxLayout(card)
        self.term_label = QtWidgets.QLabel("——")
        self.term_label.setObjectName("bigterm")
        self.term_label.setAlignment(QtCore.Qt.AlignCenter)
        card_layout.addWidget(self.term_label)

        self.mean_label = QtWidgets.QLabel("")
        self.mean_label.setAlignment(QtCore.Qt.AlignCenter)
        self.mean_label.setWordWrap(True)
        card_layout.addWidget(self.mean_label)

        btn_toggle = QtWidgets.QPushButton("显示 / 隐藏 释义")
        btn_toggle.setObjectName("secondary")
        btn_toggle.clicked.connect(self.toggle_meaning)
        card_layout.addWidget(btn_toggle, 0, QtCore.Qt.AlignHCenter)

        # 评分按钮
        rating = QtWidgets.QHBoxLayout()
        self.btn_again = QtWidgets.QPushButton("再来一次")
        self.btn_hard = QtWidgets.QPushButton("困难")
        self.btn_good = QtWidgets.QPushButton("记住了")
        self.btn_easy = QtWidgets.QPushButton("非常容易")
        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setEnabled(False)
            rating.addWidget(b)
        self.btn_again.clicked.connect(lambda: self.submit_rating(1))
        self.btn_hard.clicked.connect(lambda: self.submit_rating(3))
        self.btn_good.clicked.connect(lambda: self.submit_rating(4))
        self.btn_easy.clicked.connect(lambda: self.submit_rating(5))
        card_layout.addLayout(rating)

        v.addWidget(card, 1)

        # 显著的继续按钮
        bottom = QtWidgets.QHBoxLayout()
        self.continue_btn = QtWidgets.QPushButton("继续（Space）")
        self.continue_btn.setObjectName("primary")
        self.continue_btn.setFixedHeight(48)
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

        # 准备复习队列
        self.prepare_queue()
        if not self.queue:
            QtWidgets.QMessageBox.information(
                self, "信息",
                "当前没有到期的单词可复习（或刚录入的单元未到首次复习时间）。"
            )
            self.close()
            return
        self.show_card(self.queue[self.idx])

    def prepare_queue(self):
        # 取出所有到期卡片
        rows = list_due_cards(self.db, unit=self.unit_filter)
        self.queue = []

        n = len(rows)
        if n == 0:
            return

        # 7:3比例
        num_mode0 = int(n * 0.7)  # 日语->中文
        num_mode1 = n - num_mode0  # 中文->日语

        # 生成模式列表
        modes = [0] * num_mode0 + [1] * num_mode1
        random.shuffle(modes)

        # 生成 queue
        for r, m in zip(rows, modes):
            self.queue.append((r, m))

    def show_card(self, card):
        row, mode = card
        self.current = (row, mode)
        self.showing_mean = False
        self.mean_label.setText("")

        # 清理旧控件
        if hasattr(self, 'input_answer'):
            self.input_answer.hide()
        if hasattr(self, 'btn_confirm'):
            self.btn_confirm.hide()

        if mode == 0:  # 日语→中文
            self.term_label.setText(row[3])
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(True)
            self.continue_btn.setEnabled(False)
        else:  # 中文→日语
            self.term_label.setText(row[4])
            # 创建输入框和确认按钮
            self.input_answer = QtWidgets.QLineEdit()
            self.input_answer.setPlaceholderText("请输入日语答案")
            self.btn_confirm = QtWidgets.QPushButton("确认答案")
            self.btn_confirm.clicked.connect(self.check_answer)
            self.layout().addWidget(self.input_answer)
            self.layout().addWidget(self.btn_confirm)
            self.input_answer.show()
            self.btn_confirm.show()
            for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
                b.setEnabled(False)
            self.continue_btn.setEnabled(False)

    def check_answer(self):
        user_input = self.input_answer.text().strip()
        correct = self.current[0][3]  # 日语
        if user_input == correct:
            self.mean_label.setText("回答正确！")
            quality = 5
        else:
            self.mean_label.setText(f"你的答案: {user_input}\n正确答案: {correct}")
            quality = 3

        # 更新 SM-2 数据
        interval, repetition, ef = sm2_update(self.current[0], quality)
        last_review = datetime.utcnow().isoformat()
        due_date = (datetime.utcnow() + timedelta(days=interval)).isoformat()
        update_card_review(self.db, self.current[0][0], interval, repetition, ef, last_review, due_date)

        # 启用继续按钮
        self.continue_btn.setEnabled(True)
        self.input_answer.hide()
        self.btn_confirm.hide()

    def toggle_meaning(self):
        if not self.current:
            return
        row, mode = self.current
        if self.showing_mean:
            self.mean_label.setText("")
            self.showing_mean = False
        else:
            self.mean_label.setText(row[4] if mode == 0 else row[3])
            self.showing_mean = True

    def submit_rating(self, quality):
        if not self.current:
            return
        row, mode = self.current
        interval, repetition, ef = sm2_update(row, quality)
        last_review = datetime.utcnow().isoformat()
        due_date = (datetime.utcnow() + timedelta(days=interval)).isoformat()
        update_card_review(self.db, row[0], interval, repetition, ef, last_review, due_date)

        for b in (self.btn_again, self.btn_hard, self.btn_good, self.btn_easy):
            b.setEnabled(False)

        self.session_total += 1
        if quality >= 4:
            self.session_correct += 1
        self.continue_btn.setEnabled(True)
        self.mean_label.setText(row[4] if mode == 0 else row[3])
        self.showing_mean = True
        self.info_label.setText(f"已做 {self.session_total}，正确 {self.session_correct}")

    def next_card(self):
        self.idx += 1
        if self.idx >= len(self.queue):
            QtWidgets.QMessageBox.information(
                self, "本轮结束",
                f"本轮复习结束\n共 {self.session_total} 项，正确 {self.session_correct}"
            )
            self.close()
            return
        self.show_card(self.queue[self.idx])

    def try_space_continue(self):
        if self.continue_btn.isEnabled():
            self.next_card()

def main():
    app = QtWidgets.QApplication(sys.argv)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
