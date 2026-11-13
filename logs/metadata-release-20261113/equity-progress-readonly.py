from datetime import datetime,timezone
import json,subprocess,time
q="""SET SESSION time_zone='SYSTEM'; SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time=5000;
SELECT UTC_TIMESTAMP(6) AS utc_clock,@@transaction_read_only AS read_only,updated_at,JSON_LENGTH(payload,'$.errors') AS venue_error_count,JSON_LENGTH(payload,'$.symbols') AS underlying_count,JSON_EXTRACT(payload,'$.coverage.Gate.status') AS gate_status,JSON_EXTRACT(payload,'$.coverage.Bitget.status') AS bitget_status,JSON_EXTRACT(payload,'$.coverage.Bybit.status') AS bybit_status,JSON_EXTRACT(payload,'$.checked_at_epoch') AS checked_at_epoch FROM equity_spot_snapshot;
SELECT j.venue,COUNT(*) AS quoted_pairs,MIN(j.verified) AS earliest_verified_epoch,MAX(j.verified) AS latest_verified_epoch,MIN(j.captured) AS oldest_quote_epoch,MAX(j.captured) AS newest_quote_epoch FROM equity_spot_snapshot,JSON_TABLE(payload,'$.symbols[*].rows[*]' COLUMNS(venue VARCHAR(16) PATH '$.long_exchange',verified DOUBLE PATH '$.verified_at_epoch',captured DOUBLE PATH '$.captured_at_epoch')) AS j GROUP BY j.venue;"""
r={'started_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'Only public equity snapshot status/count/timestamp aggregates; no symbols, prices or private data','samples':[]}
for i in range(2):
 out=subprocess.check_output(['mysql','--batch','--raw','--database=arbitrage_hub','-e',q],text=True,timeout=12)
 r['samples'].append({'at_utc':datetime.now(timezone.utc).isoformat(),'aggregate_tsv':out})
 if i==0:time.sleep(20)
r['finished_at_utc']=datetime.now(timezone.utc).isoformat()
print(json.dumps(r,sort_keys=True))
