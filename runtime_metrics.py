"""Fixed-category rolling aggregates in memory. No request bodies, identifiers or disk writes."""
import threading
import time

CATEGORIES=frozenset({"query","save","task","other","tracking_provider","tracking_wait"})
BOUNDS=(100,300,1000,3000,10000,30000)


class RuntimeMetrics:
    def __init__(self):
        self.lock=threading.Lock(); self.hours={}; self.started_at=time.time()

    def observe(self,category,milliseconds,failed=False):
        if category not in CATEGORIES: return
        value=max(0,min(86400000,int(milliseconds)))
        hour=int(time.time()//3600)
        with self.lock:
            self.hours={h:data for h,data in self.hours.items() if hour-23<=h<=hour}
            entry=self.hours.setdefault(hour,{}).setdefault(category,{"count":0,"failed":0,"sum_ms":0,"max_ms":0,"buckets":[0]*7})
            entry["count"]+=1; entry["failed"]+=int(bool(failed)); entry["sum_ms"]+=value; entry["max_ms"]=max(entry["max_ms"],value)
            entry["buckets"][sum(value>b for b in BOUNDS)]+=1

    def summary(self):
        hour=int(time.time()//3600); output={}
        with self.lock:
            for h,data in self.hours.items():
                if h<hour-23 or h>hour: continue
                for category,value in data.items():
                    row=output.setdefault(category,{"count":0,"failed":0,"sum_ms":0,"max_ms":0,"buckets":[0]*7})
                    for key in ("count","failed","sum_ms"): row[key]+=value[key]
                    row["max_ms"]=max(row["max_ms"],value["max_ms"])
                    row["buckets"]=[a+b for a,b in zip(row["buckets"],value["buckets"])]
        for row in output.values(): row["mean_ms"]=round(row.pop("sum_ms")/max(1,row["count"]),1)
        return {"scope":"process_last_24_hour_buckets","reset_at":self.started_at,
                "window_started_at":max(self.started_at,(hour-23)*3600),
                "bucket_upper_ms":[*BOUNDS,None],"categories":output}


METRICS=RuntimeMetrics()


def request_category(method,path):
    if path=="/api/health": return None
    if path.startswith(("/api/export/","/api/admin/system/")) or path in {"/api/admin/backup.db","/api/admin/restore-db"}: return "other"
    if path.startswith(("/api/tracking/tasks/","/api/admin/shipping-batches")): return "task"
    if method=="GET": return "query"
    if path.startswith(("/api/shipments","/api/returns")) and not any(x in path for x in ("/label/","/tracking/")): return "save"
    if "tracking" in path or "shipping-batches" in path: return "task"
    return "other"
