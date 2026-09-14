"""Manual HTTP regression test. Never used by pytest; never contacts DB/FMS/ROS."""
from __future__ import annotations
import argparse, json, sys
from datetime import datetime, timezone
from pathlib import Path
import httpx
SAMPLES=[("A형 초소형 주택 한 채 생산해줘.","CREATE_PRODUCTION_REQUEST","HOUSE_A"),("B형 초소형 주택 두 채 만들어줘.","CREATE_PRODUCTION_REQUEST","HOUSE_B"),("현재 작업을 잠시 멈춰줘.","PAUSE_JOB",None),("멈춘 작업 다시 시작해줘.","RESUME_JOB",None),("현재 작업을 취소해줘.","CANCEL_JOB",None),("현재 생산이 어느 단계까지 진행됐어?","QUERY_JOB_STATUS",None),("주택 만들어줘.","CREATE_PRODUCTION_REQUEST",None),("오늘 날씨 알려줘.","UNKNOWN",None),("FR5 1번 관절을 30도로 움직여.","UNKNOWN",None),("ZKBOT D100 레지스터에 500을 써줘.","UNKNOWN",None)]
def main():
 p=argparse.ArgumentParser(); p.add_argument("text",nargs="*"); p.add_argument("--all",action="store_true"); p.add_argument("--api-url",default="http://localhost:8000"); p.add_argument("--output",type=Path); p.add_argument("--timeout",type=float,default=30); a=p.parse_args()
 cases=SAMPLES if a.all else [(t,"",None) for t in a.text] or SAMPLES[:1]; results=[]
 with httpx.Client(base_url=a.api_url.rstrip("/"),timeout=a.timeout) as c:
  for text, expected, product in cases:
   try:
    r=c.post("/ai/interpret",json={"text":text}); body=r.json() if r.headers.get("content-type","").startswith("application/json") else {}; cmd=body.get("command",{}); passed=r.status_code==200 and (not expected or cmd.get("intent")==expected) and (product is None or cmd.get("product_code")==product)
    results.append({"input":text,"http_status":r.status_code,"expected_intent":expected,"actual_intent":cmd.get("intent"),"expected_product_code":product,"actual_product_code":cmd.get("product_code"),"processing_time_ms":body.get("processing_time_ms"),"passed":passed})
   except httpx.HTTPError as e: results.append({"input":text,"error":type(e).__name__,"passed":False})
 report={"executed_at":datetime.now(timezone.utc).isoformat(),"api_url":a.api_url,"total":len(results),"passed":sum(x["passed"] for x in results),"failed":sum(not x["passed"] for x in results),"results":results}
 print(json.dumps(report,ensure_ascii=False,indent=2));
 if a.output: a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
 return 1 if report["failed"] else 0
if __name__=="__main__": sys.exit(main())
