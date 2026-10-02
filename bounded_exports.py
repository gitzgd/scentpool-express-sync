"""Consistent, private temporary export snapshots and bounded row/file generation."""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import shutil
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from database import Database, AppError

MAX_EXPORT_BYTES = 128 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024
EXPORT_SECONDS = 180


def prepare_export(db, user, filters, directory, extension, *, headers, widths, row_builder, template_builder, cell_builder):
    """No customer data leaves the existing authenticated response; caller owns TemporaryDirectory."""
    started=time.monotonic(); directory=Path(directory)
    snapshot=directory/"snapshot.db"
    output=directory/("shipments."+extension)
    def check_size(path=None):
        if time.monotonic()-started>EXPORT_SECONDS:
            raise AppError("导出超过安全时间，请缩小日期范围后重试。订单未被修改。",503)
        if path and path.exists() and path.stat().st_size>MAX_EXPORT_BYTES:
            raise AppError("导出文件较大，请缩小日期范围后重试。订单未被修改。",413)
    # SQLite online backup releases its source read lock between bounded steps, unlike a long export SELECT.
    with db.connect_readonly() as source:
        pages=source.execute("PRAGMA page_count").fetchone()[0]
        size=source.execute("PRAGMA page_size").fetchone()[0]
        if pages*size>MAX_SNAPSHOT_BYTES:
            raise AppError("数据库超出在线导出安全范围，请联系管理员。",413)
        if shutil.disk_usage(directory).free < pages*size + MAX_EXPORT_BYTES + 64*1024*1024:
            raise AppError("临时存储空间不足，本次导出未开始，请联系管理员。订单未被修改。",503)
        destination=sqlite3.connect(snapshot)
        try:
            os.chmod(snapshot,0o600)
            source.backup(destination,pages=128,progress=lambda *_:check_size(),sleep=.01)
        finally:
            destination.close()
    local=Database(str(snapshot))
    store_names=set()
    row_count=0
    def rows():
        nonlocal row_count
        # The connection belongs to the private immutable snapshot, not the live WAL database.
        with local.connect_readonly() as conn:
            offset=0
            while True:
                check_size(output)
                chunk=local._list_shipments_with_connection(conn,user,filters,limit=50,offset=offset)
                if not chunk: break
                for record in chunk:
                    if len(store_names)<2:
                        store_names.add(str(record.get("store_name_snapshot") or ""))
                    row_count+=1
                yield from row_builder(chunk)
                offset+=len(chunk)
    if extension=="csv":
        with output.open("w",encoding="utf-8-sig",newline="") as handle:
            writer=csv.writer(handle); writer.writerow(headers)
            for row in rows(): writer.writerow(row)
    elif extension=="xlsx":
        # Reuse the existing small workbook/style template, replacing only the worksheet with a stream.
        template=template_builder(headers,[],widths,"发货明细")
        with zipfile.ZipFile(io.BytesIO(template)) as initial, zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as result:
            for name in initial.namelist():
                if name!="xl/worksheets/sheet1.xml": result.writestr(name,initial.read(name))
            with result.open("xl/worksheets/sheet1.xml","w",force_zip64=True) as sheet:
                sheet.write(('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>'+
                    ''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i,w in enumerate(widths,1))+
                    '</cols><sheetData>').encode())
                def write_row(index,row):
                    sheet.write((f'<row r="{index}">'+''.join(cell_builder(index,i,value,1 if index==1 else 2) for i,value in enumerate(row,1))+'</row>').encode())
                write_row(1,headers)
                for index,row in enumerate(rows(),2): write_row(index,row)
                # Excel's sheet row limit is checked before producing an invalid artifact.
                if row_count>=1048576:
                    raise AppError("导出超过 Excel 单页上限，请缩小日期范围。",413)
                from server import excel_col
                sheet.write(f'</sheetData><autoFilter ref="A1:{excel_col(len(headers))}{row_count+1}"/></worksheet>'.encode())
    else:
        raise AppError("不支持的导出格式。")
    os.chmod(output,0o600); check_size(output)
    # Return only a minimal filename hint; never return a full in-memory copy of the export.
    hint=[{"store_name_snapshot":next(iter(store_names))}] if len(store_names)==1 else []
    return output,hint,row_count
