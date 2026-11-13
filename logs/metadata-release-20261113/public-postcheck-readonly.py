"""Read-only public snapshot/retention aggregates; no row content exported."""
import json
import subprocess
from datetime import datetime, timezone

prefix = "SET SESSION time_zone='SYSTEM'; SET SESSION TRANSACTION READ ONLY; SET SESSION max_execution_time=5000; "
queries = {
    'price_history': """SELECT UTC_TIMESTAMP(6) AS utc_clock,@@transaction_read_only AS read_only,
      UNIX_TIMESTAMP() AS now_epoch,COUNT(*) AS rows_total,
      MIN(bucket_at) AS oldest_bucket_epoch,MAX(bucket_at) AS newest_bucket_epoch,
      SUM(bucket_at < UNIX_TIMESTAMP()-691200) AS outside_current_eight_day_window,
      SUM(bucket_at >= 1794566700) AS rows_since_start_bucket
      FROM futures_price_history;""",
    'equity_status': """SELECT UTC_TIMESTAMP(6) AS utc_clock,@@transaction_read_only AS read_only,
      updated_at,JSON_LENGTH(payload,'$.errors') AS venue_error_count,
      JSON_EXTRACT(payload,'$.coverage.Gate.status') AS gate_status,
      JSON_EXTRACT(payload,'$.coverage.Bitget.status') AS bitget_status,
      JSON_EXTRACT(payload,'$.coverage.Bybit.status') AS bybit_status,
      CASE
       WHEN JSON_EXTRACT(payload,'$.errors.Gate') IS NULL THEN 'none'
       WHEN JSON_UNQUOTE(JSON_EXTRACT(payload,'$.errors.Gate')) LIKE 'equity_gate_pairs%' THEN 'gate_pairs_not_ready'
       WHEN JSON_UNQUOTE(JSON_EXTRACT(payload,'$.errors.Gate')) LIKE 'equity_gate_coins%' THEN 'gate_coins_not_ready'
       WHEN JSON_UNQUOTE(JSON_EXTRACT(payload,'$.errors.Gate')) LIKE 'gate_spot%' THEN 'gate_spot_not_ready'
       ELSE 'other_public_input_error'
      END AS gate_error_category
      FROM equity_spot_snapshot;""",
}
result = {'started_at_utc':datetime.now(timezone.utc).isoformat(),
          'boundary':'Public counts/timestamps and fixed error categories only. Counts do not prove every historical row deletion reason.'}
for name, query in queries.items():
    output = subprocess.check_output(['mysql','--batch','--raw','--database=arbitrage_hub','-e',prefix+query],text=True,timeout=12).splitlines()
    result[name] = [dict(zip(output[0].split('\t'),row.split('\t'))) for row in output[1:]]
result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
print(json.dumps(result,sort_keys=True))
