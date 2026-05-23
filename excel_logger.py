import os
import json
import subprocess
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

EXCEL_FILE = "pnl_log.xlsx"
_BASE = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(_BASE, "docs")

HEADERS = [
    "Thời gian", "Coin", "Giá vào ($)", "Giá hiện tại ($)",
    "Vốn vào ($)", "Funding PnL ($)", "Giá PnL ($)",
    "Tổng PnL ($)", "% Lãi", "Số lần nhận funding", "Số dư USDT ($)",
]
COL_WIDTHS = [20, 8, 14, 16, 13, 15, 13, 14, 9, 21, 16]


def _get_or_create_wb():
    if os.path.exists(EXCEL_FILE):
        return openpyxl.load_workbook(EXCEL_FILE)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Lời Lỗ"
    _write_header(ws)
    wb.save(EXCEL_FILE)
    return wb


def _write_header(ws):
    hfill = PatternFill("solid", fgColor="1F4E79")
    hfont = Font(bold=True, color="FFFFFF", size=11)
    for col, (h, w) in enumerate(zip(HEADERS, COL_WIDTHS), 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = hfill
        cell.font = hfont
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"


def log_pnl_snapshot(positions: list[dict], pnl_list: list[dict], usdt_balance: float):
    """
    positions : list các dict từ open_position()
    pnl_list  : list kết quả estimate_pnl() tương ứng (có thể None nếu lỗi)
    usdt_balance : số dư USDT hiện tại
    """
    wb = _get_or_create_wb()
    ws = wb["Lời Lỗ"]

    if ws.cell(1, 1).value != HEADERS[0]:
        _write_header(ws)

    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    valid = [(p, pnl) for p, pnl in zip(positions, pnl_list) if pnl is not None]

    if not valid:
        return

    for i, (pos, pnl) in enumerate(valid):
        row = ws.max_row + 1
        usdt_in = pos['contracts'] * pos['ct_val'] * pos['entry_price']
        pct = (pnl['total_pnl'] / usdt_in * 100) if usdt_in > 0 else 0
        is_last = (i == len(valid) - 1)

        values = [
            now_str,
            pos['coin'],
            round(pos['entry_price'], 4),
            round(pnl['price'], 4),
            round(usdt_in, 2),
            round(pnl['funding_pnl'], 4),
            round(pnl['price_pnl'], 4),
            round(pnl['total_pnl'], 4),
            round(pct, 3),
            pnl['n_payments'],
            round(usdt_balance, 2) if is_last else "",
        ]

        green = pnl['total_pnl'] >= 0
        pnl_fill  = PatternFill("solid", fgColor="C6EFCE" if green else "FFC7CE")
        pnl_color = "276221" if green else "9C0006"

        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row, column=col, value=val)
            cell.alignment = Alignment(horizontal="center")
            if col in (6, 7, 8):
                cell.fill = pnl_fill
                cell.font = Font(color=pnl_color, bold=(col == 8))

    # Dòng tổng kết nếu có nhiều vị thế
    if len(valid) > 1:
        row = ws.max_row + 1
        total_pnl    = sum(pnl['total_pnl'] for _, pnl in valid)
        total_usdt   = sum(p['contracts'] * p['ct_val'] * p['entry_price'] for p, _ in valid)
        total_pct    = (total_pnl / total_usdt * 100) if total_usdt > 0 else 0
        sfill = PatternFill("solid", fgColor="D9E1F2")
        green = total_pnl >= 0

        summary = [
            now_str, "TỔNG", "", "",
            round(total_usdt, 2), "", "",
            round(total_pnl, 4),
            round(total_pct, 3), "",
            round(usdt_balance, 2),
        ]
        for col, val in enumerate(summary, 1):
            cell = ws.cell(row=row, column=col, value=val)
            cell.fill = sfill
            cell.alignment = Alignment(horizontal="center")
            if col == 2:
                cell.font = Font(bold=True)
            if col == 8:
                cell.font = Font(bold=True, color="276221" if green else "9C0006")

    # Dòng trắng phân cách giữa các kỳ
    ws.append([""] * len(HEADERS))

    wb.save(EXCEL_FILE)


def export_json():
    """Đọc Excel → xuất docs/data.json để GitHub Pages hiển thị."""
    os.makedirs(DOCS_DIR, exist_ok=True)
    json_path = os.path.join(DOCS_DIR, "data.json")

    excel_path = os.path.join(_BASE, EXCEL_FILE)
    if not os.path.exists(excel_path):
        return

    wb  = openpyxl.load_workbook(excel_path, data_only=True)
    ws  = wb.active
    hdrs = [c.value for c in ws[1] if c.value]
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if any(v is not None and v != "" for v in row):
            rows.append([str(v) if v is not None else "" for v in row[:len(hdrs)]])
    wb.close()

    coin_i = hdrs.index("Coin")          if "Coin"          in hdrs else -1
    pnl_i  = hdrs.index("Tổng PnL ($)") if "Tổng PnL ($)"  in hdrs else -1
    time_i = hdrs.index("Thời gian")    if "Thời gian"     in hdrs else -1
    bal_i  = hdrs.index("Số dư USDT ($)") if "Số dư USDT ($)" in hdrs else -1

    # Dùng dòng TỔNG cho chart (fallback: dùng tất cả dòng không phải TỔNG)
    sum_rows = [r for r in rows if coin_i >= 0 and r[coin_i] == "TỔNG"]
    if not sum_rows:
        sum_rows = [r for r in rows if coin_i >= 0 and r[coin_i] not in ("", "TỔNG")]

    cum = 0
    labels, values = [], []
    for r in sum_rows:
        v = float(r[pnl_i]) if pnl_i >= 0 and r[pnl_i] else 0
        cum += v
        labels.append(r[time_i] if time_i >= 0 else "")
        values.append(round(cum, 4))

    # Số dư USDT cuối cùng
    last_bal = ""
    if bal_i >= 0:
        for r in reversed(rows):
            if r[bal_i]:
                last_bal = r[bal_i]
                break

    data = {
        "updated_at":    datetime.now().strftime("%d/%m/%Y %H:%M"),
        "total_periods": len(sum_rows),
        "total_pnl":     round(cum, 4),
        "last_balance":  last_bal,
        "headers":       hdrs,
        "rows":          rows,
        "chart":         {"labels": labels, "values": values},
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def push_to_github():
    """Git add data.json → commit → push lên GitHub Pages."""
    try:
        subprocess.run(
            ["git", "-C", _BASE, "add", "docs/data.json"],
            check=True, capture_output=True,
        )
        res = subprocess.run(
            ["git", "-C", _BASE, "commit", "-m",
             f"update: pnl data {datetime.now().strftime('%d/%m %H:%M')}"],
            capture_output=True, text=True,
        )
        if "nothing to commit" in (res.stdout + res.stderr):
            return True
        subprocess.run(
            ["git", "-C", _BASE, "push"],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
