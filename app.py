"""Finance CloudOps - a simple FinOps base project.

Track cloud costs, set team budgets, detect cost spikes, view a dashboard.
Run:  python app.py   ->  http://127.0.0.1:5000
"""
import csv
import io
import sqlite3
import statistics
from datetime import date, timedelta
from random import Random

from flask import Flask, g, jsonify, render_template_string, request

DB_PATH = "cloudops.db"
app = Flask(__name__)

# ---------- database helpers ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS costs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,            -- YYYY-MM-DD
        provider TEXT NOT NULL,       -- aws / azure / gcp
        service TEXT NOT NULL,        -- EC2, S3, ...
        team TEXT NOT NULL,
        amount REAL NOT NULL          -- USD
    );
    CREATE TABLE IF NOT EXISTS budgets (
        team TEXT PRIMARY KEY,
        monthly_limit REAL NOT NULL
    );
    """)
    db.commit()
    db.close()


def rows(query, args=()):
    return [dict(r) for r in get_db().execute(query, args).fetchall()]


# ---------- API: costs ----------
@app.post("/api/costs")
def add_cost():
    d = request.get_json(force=True)
    try:
        rec = (d["day"], d["provider"], d["service"], d["team"], float(d["amount"]))
    except (KeyError, ValueError):
        return jsonify(error="need day, provider, service, team, amount"), 400
    db = get_db()
    db.execute("INSERT INTO costs(day,provider,service,team,amount) VALUES (?,?,?,?,?)", rec)
    db.commit()
    return jsonify(ok=True), 201


@app.get("/api/costs")
def list_costs():
    return jsonify(rows("SELECT * FROM costs ORDER BY day DESC LIMIT 200"))


@app.post("/api/costs/import")
def import_csv():
    """Upload CSV with header: day,provider,service,team,amount"""
    f = request.files.get("file")
    if not f:
        return jsonify(error="attach a CSV as 'file'"), 400
    reader = csv.DictReader(io.StringIO(f.read().decode("utf-8")))
    db, n = get_db(), 0
    for r in reader:
        db.execute("INSERT INTO costs(day,provider,service,team,amount) VALUES (?,?,?,?,?)",
                   (r["day"], r["provider"], r["service"], r["team"], float(r["amount"])))
        n += 1
    db.commit()
    return jsonify(imported=n)


# ---------- API: budgets ----------
@app.put("/api/budgets/<team>")
def set_budget(team):
    limit = float(request.get_json(force=True)["monthly_limit"])
    db = get_db()
    db.execute("INSERT INTO budgets(team,monthly_limit) VALUES (?,?) "
               "ON CONFLICT(team) DO UPDATE SET monthly_limit=excluded.monthly_limit", (team, limit))
    db.commit()
    return jsonify(team=team, monthly_limit=limit)


@app.get("/api/budgets")
def budget_status():
    month = date.today().strftime("%Y-%m")
    spend = {r["team"]: r["total"] for r in rows(
        "SELECT team, SUM(amount) total FROM costs WHERE day LIKE ? GROUP BY team", (month + "%",))}
    out = []
    for b in rows("SELECT * FROM budgets"):
        used = spend.get(b["team"], 0.0)
        pct = used / b["monthly_limit"] * 100 if b["monthly_limit"] else 0
        status = "OVER" if pct >= 100 else "WARNING" if pct >= 80 else "OK"
        out.append({**b, "spent": round(used, 2), "percent": round(pct, 1), "status": status})
    return jsonify(out)


# ---------- API: reports ----------
@app.get("/api/summary")
def summary():
    return jsonify(
        by_service=rows("SELECT service, ROUND(SUM(amount),2) total FROM costs GROUP BY service ORDER BY total DESC"),
        by_team=rows("SELECT team, ROUND(SUM(amount),2) total FROM costs GROUP BY team ORDER BY total DESC"),
        by_provider=rows("SELECT provider, ROUND(SUM(amount),2) total FROM costs GROUP BY provider ORDER BY total DESC"),
        daily=rows("SELECT day, ROUND(SUM(amount),2) total FROM costs GROUP BY day ORDER BY day DESC LIMIT 30"),
    )


@app.get("/api/anomalies")
def anomalies():
    """Flag days whose total spend is > 2 std-devs above the mean."""
    daily = rows("SELECT day, SUM(amount) total FROM costs GROUP BY day ORDER BY day")
    if len(daily) < 7:
        return jsonify([])
    totals = [d["total"] for d in daily]
    mean, sd = statistics.mean(totals), statistics.pstdev(totals)
    return jsonify([{**d, "total": round(d["total"], 2), "mean": round(mean, 2)}
                    for d in daily if sd and d["total"] > mean + 2 * sd])


@app.get("/api/forecast")
def forecast():
    """Naive month-end forecast: average daily spend * days in month."""
    today = date.today()
    month = today.strftime("%Y-%m")
    spent = rows("SELECT COALESCE(SUM(amount),0) t FROM costs WHERE day LIKE ?", (month + "%",))[0]["t"]
    nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_in_month = (nxt - timedelta(days=1)).day
    return jsonify(spent_so_far=round(spent, 2),
                   forecast_month_end=round(spent / today.day * days_in_month, 2))


# ---------- demo data ----------
@app.post("/api/seed")
def seed():
    rnd, db = Random(42), get_db()
    services = [("aws", "EC2", 120), ("aws", "S3", 40), ("azure", "VMs", 90), ("gcp", "BigQuery", 70)]
    teams = ["platform", "data", "web"]
    for i in range(45):
        day = (date.today() - timedelta(days=i)).isoformat()
        for prov, svc, base in services:
            for t in teams:
                amt = base * rnd.uniform(0.5, 1.5) / 3
                if i == 5 and svc == "EC2":
                    amt *= 6  # planted spike
                db.execute("INSERT INTO costs(day,provider,service,team,amount) VALUES (?,?,?,?,?)",
                           (day, prov, svc, t, round(amt, 2)))
    for t, lim in [("platform", 2500), ("data", 2000), ("web", 1500)]:
        db.execute("INSERT OR REPLACE INTO budgets VALUES (?,?)", (t, lim))
    db.commit()
    return jsonify(seeded=True)


# ---------- dashboard ----------
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Finance CloudOps</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1b2430}
 header{background:#0b3d63;color:#fff;padding:16px 28px;display:flex;justify-content:space-between;align-items:center}
 main{padding:24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}
 .card{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 4px #0001}
 h2{margin:0 0 10px;font-size:1rem}
 .row{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:.9rem}
 .row span:first-child{width:90px}
 .bar{height:10px;background:#2b8cbe;border-radius:5px}
 .OK{color:#1a7f37}.WARNING{color:#b58105}.OVER{color:#cf222e;font-weight:700}
 button{padding:8px 14px;border:0;border-radius:6px;cursor:pointer}
 .big{font-size:1.8rem;font-weight:700}
</style></head><body>
<header><h1>Finance CloudOps</h1><button onclick="seed()">Load demo data</button></header>
<main>
 <div class="card"><h2>Month forecast</h2><div class="big" id="fc">-</div><div id="sp"></div></div>
 <div class="card"><h2>Budgets (this month)</h2><div id="budgets"></div></div>
 <div class="card"><h2>Spend by service</h2><div id="svc"></div></div>
 <div class="card"><h2>Spend by team</h2><div id="team"></div></div>
 <div class="card"><h2>Spend by provider</h2><div id="prov"></div></div>
 <div class="card"><h2>Cost anomalies</h2><div id="anom"></div></div>
</main>
<script>
const $=id=>document.getElementById(id), j=u=>fetch(u).then(r=>r.json());
const money=n=>"$"+Number(n).toLocaleString(undefined,{maximumFractionDigits:0});
function bars(el,data,key){const m=Math.max(...data.map(d=>d.total),1);
 $(el).innerHTML=data.map(d=>`<div class="row"><span>${d[key]}</span>
 <div class="bar" style="width:${d.total/m*60}%"></div><span>${money(d.total)}</span></div>`).join("")||"No data";}
async function load(){
 const s=await j("/api/summary");
 bars("svc",s.by_service,"service");bars("team",s.by_team,"team");bars("prov",s.by_provider,"provider");
 const f=await j("/api/forecast");
 $("fc").textContent=money(f.forecast_month_end);$("sp").textContent="Spent so far: "+money(f.spent_so_far);
 const b=await j("/api/budgets");
 $("budgets").innerHTML=b.map(x=>`<div class="row"><span>${x.team}</span>
  <span>${money(x.spent)} / ${money(x.monthly_limit)}</span><span class="${x.status}">${x.percent}% ${x.status}</span></div>`).join("")||"No budgets";
 const a=await j("/api/anomalies");
 $("anom").innerHTML=a.map(x=>`<div class="row"><span>${x.day}</span><span class="OVER">${money(x.total)}</span><span>(avg ${money(x.mean)})</span></div>`).join("")||"None detected";
}
async function seed(){await fetch("/api/seed",{method:"POST"});load();}
load();
</script></body></html>"""


@app.get("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
