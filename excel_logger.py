import os
from datetime import datetime
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

EXCEL_FILE = "pnl_log.xlsx"

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
