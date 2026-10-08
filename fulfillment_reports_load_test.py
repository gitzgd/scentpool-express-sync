"""Optional bounded load check using only disposable synthetic records."""
import json
import resource
import sys
import tempfile
import time
from pathlib import Path
from database import Database
from fulfillment_reports_test import seed, configure, shipment, RANGE
from fulfillment_reports import read_report, export_report


def main():
    with tempfile.TemporaryDirectory(prefix="report-load-") as directory:
        db=Database(str(Path(directory)/"synthetic.db")); users=seed(db); configure(db,users)
        row,job=shipment(db,users,"BASE")
        with db.connect() as conn:
            cols=[r[1] for r in conn.execute("PRAGMA table_info(shipments)") if r[1]!="id"]
            select=["?" if c in {"business_id","store_order_no","booking_request_id"} else '"'+c+'"' for c in cols]
            sql='INSERT INTO shipments ('+','.join('"'+c+'"' for c in cols)+') SELECT '+','.join(select)+' FROM shipments WHERE id=?'
            for i in range(12000):
                sid=conn.execute(sql,(f"LOAD-{i}",f"LOAD-{i}",f"REQ-{i}",row["id"])).lastrowid
                conn.execute("INSERT INTO shipment_items(shipment_id,product_barcode,product_name,product_category,quantity) VALUES (?,?,?,?,3)",(sid,f"000{i%200:04}","合成商品"+str(i%200),"合成"))
                conn.execute("INSERT INTO shipping_batch_items(batch_id,shipment_id,request_id,status,settings_snapshot_json,created_at,updated_at) VALUES (?,?,?,'成功',?,'x','x')",(job["batch_id"],sid,f"REQ-{i}",json.dumps({"profile_id":"banna" if i%2 else "kunming"})))
        def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024**2 if sys.platform=="darwin" else 1024)
        times=[]; before=rss()
        for n in range(6):
            start=time.monotonic(); result=read_report(db,users["admin"],{**RANGE,"view":"daily_products" if n%2 else "summary"})
            times.append(round(time.monotonic()-start,3)); assert result["summary"]["orders"]==12001
            assert len(result["rows"])<=50 and result["summary"]["product_quantity"]==36003
        query_peak=rss()
        start=time.monotonic();output,_=export_report(db,users["admin"],RANGE,directory)
        print(json.dumps({"orders":12001,"query_seconds":times,"export_seconds":round(time.monotonic()-start,3),
            "export_bytes":output.stat().st_size,"rss_before_mib":round(before,2),"rss_after_queries_mib":round(query_peak,2),
            "rss_after_export_mib":round(rss(),2)},ensure_ascii=False))


if __name__=="__main__": main()
