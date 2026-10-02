"""Full-filter, consistent snapshot and bounded export regressions using synthetic records."""
import csv
import io
import json
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import server
from bounded_exports import prepare_export
from database import Database, AppError
from special_shipments_test import seed
from reliability_test import shipment
from runtime_metrics import RuntimeMetrics, request_category


def run():
    with tempfile.TemporaryDirectory(prefix="scentpool-export-test-") as directory:
        db=Database(str(Path(directory)/"source.db")); users=seed(db)
        for i in range(121): shipment(db,users,str(i))
        arguments=dict(headers=server.EXPORT_HEADERS,widths=server.EXPORT_COLUMN_WIDTHS,
            row_builder=server.export_rows,template_builder=server.build_table_xlsx,cell_builder=server.xlsx_cell)
        original=Database._list_shipments_with_connection
        observed=[]
        def bounded(self,conn,user,filters,*,limit=None,offset=0):
            assert self.path!=db.path, "must never pin the live WAL for the export"
            assert limit==50
            observed.append(offset)
            result=original(self,conn,user,filters,limit=limit,offset=offset)
            if offset==0:
                # Concurrent live updates cannot cause later pages to move/change in the export snapshot.
                with db.connect() as writer: writer.execute("UPDATE shipments SET remark='after-snapshot' WHERE id=1")
            return result
        with patch.object(Database,"_list_shipments_with_connection",bounded):
            for extension in ("csv","xlsx"):
                with tempfile.TemporaryDirectory(dir=directory) as output:
                    path,hint,count=prepare_export(db,users["admin"],{"page":2,"page_size":1},output,extension,**arguments)
                    assert count==121 and hint
                    if extension=="csv":
                        rows=list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
                        assert len(rows)==121 and rows[-1]["备注"]==""
                    else:
                        with zipfile.ZipFile(path) as archive:
                            xml=ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                            assert len(xml.findall("{*}sheetData/{*}row"))==122
        assert observed==[0,50,100,121]*2,observed
        with tempfile.TemporaryDirectory(dir=directory) as output, patch("bounded_exports.MAX_SNAPSHOT_BYTES",1):
            try: prepare_export(db,users["admin"],{},output,"csv",**arguments)
            except AppError as exc: assert exc.status==413
            else: raise AssertionError("oversize snapshot should fail before copying")
        assert db.connection_diagnostics()["active"]==0
        stats=RuntimeMetrics()
        for index in range(1000): stats.observe("query",index,index%3==0)
        stats.observe("recipient-secret",1)
        result=stats.summary()
        assert set(result["categories"])=={"query"}
        assert result["categories"]["query"]["count"]==1000
        assert len(result["categories"]["query"]["buckets"])==7
        assert "recipient-secret" not in json.dumps(result)
        assert request_category("GET","/api/health") is None
        assert request_category("GET","/api/export/shipments.xlsx")=="other"
        assert request_category("GET","/api/admin/backup.db")=="other"
        assert request_category("GET","/api/admin/system/diagnostics")=="other"
        assert request_category("GET","/api/tracking/tasks/local-synthetic")=="task"
        assert request_category("GET","/api/shipments")=="query"
        assert request_category("POST","/api/returns")=="save"
        with patch("runtime_metrics.time.time",return_value=__import__('time').time()+25*3600):
            stats.observe("save",1)
            assert set(stats.summary()["categories"])=={"save"} and len(stats.hours)==1
        # Health polling is not employee latency and must not pollute either
        # runtime buckets or slow-request logs, even with a zero logging threshold.
        stats=RuntimeMetrics()
        handler=object.__new__(server.Handler)
        handler.command="GET"; handler.route_api=lambda *_args:None
        with patch.object(server,"METRICS",stats),patch.object(server,"SLOW_REQUEST_MILLISECONDS",0),patch("builtins.print") as output:
            handler.path="/api/health"; handler.route()
            assert stats.summary()["categories"]=={} and not output.called
            handler.path="/api/returns/summary"; handler.route()
            assert stats.summary()["categories"]["query"]["count"]==1
            assert output.call_count==1
    print("bounded exports: full filtered scope, 50-row batches, live-write snapshot isolation, cap/cleanup and fixed metrics PASS")


if __name__=="__main__": run()
