import os, time, requests, numpy as np, pandas as pd
from datetime import datetime, timezone

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CG = "https://api.coingecko.com/api/v3"
OKX = "https://www.okx.com/api/v5"
TIMEOUT, RETRIES = 20, 3
MIN_VOL, MIN_SCORE, DEEP = 5_000_000, 85, 50
S = requests.Session()
S.headers.update({"User-Agent":"AKAM-AI-SMART-SCANNER/2.0","Accept":"application/json"})

def get(url, params=None):
    err = None
    for n in range(RETRIES):
        try:
            r = S.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(2+n); continue
            r.raise_for_status()
            x = r.json()
            if isinstance(x,dict) and "code" in x and str(x["code"]) != "0":
                raise RuntimeError(f"API {x.get('code')}: {x.get('msg')}")
            return x
        except Exception as e:
            err=e; time.sleep(1.5*(n+1))
    raise RuntimeError(f"request failed: {url}: {err}")

def tg(msg):
    r=S.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
             data={"chat_id":CHAT_ID,"text":msg,"disable_web_page_preview":"true"},
             timeout=TIMEOUT)
    r.raise_for_status()

def universe():
    out=[]
    for p in range(1,5):
        rows=get(f"{CG}/coins/markets",{"vs_currency":"usd","order":"market_cap_desc",
            "per_page":250,"page":p,"sparkline":"false"})
        for x in rows:
            sym=str(x.get("symbol","")).upper().strip()
            if sym:
                out.append({"rank":len(out)+1,"symbol":sym,"name":x.get("name",sym),
                            "market_cap":float(x.get("market_cap") or 0)})
        if len(rows)<250: break
        time.sleep(.2)
    seen=set(); clean=[]
    for x in out:
        if x["symbol"] in seen: continue
        seen.add(x["symbol"]); clean.append(x)
        if len(clean)>=1000: break
    return clean

def instruments():
    rows=get(f"{OKX}/public/instruments",{"instType":"SPOT"})["data"]
    return {x["instId"][:-5]:x["instId"] for x in rows
            if x.get("state")=="live" and x.get("instId","").endswith("-USDT")}

def tickers():
    rows=get(f"{OKX}/market/tickers",{"instType":"SPOT"})["data"]
    d={}
    for x in rows:
        i=x.get("instId","")
        if not i.endswith("-USDT"): continue
        try:
            d[i[:-5]]={"instId":i,"price":float(x["last"]),
                       "vol":float(x.get("volCcy24h") or 0)}
        except: pass
    return d

def candles(inst, bar, limit=220):
    rows=get(f"{OKX}/market/candles",{"instId":inst,"bar":bar,"limit":min(limit,300)})["data"]
    a=[]
    for x in rows:
        if len(x)<5: continue
        try:
            a.append([int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),
                      float(x[5]) if len(x)>5 else 0,
                      float(x[7]) if len(x)>7 else 0,
                      str(x[8]) if len(x)>8 else "1"])
        except: pass
    d=pd.DataFrame(a,columns=["ts","open","high","low","close","volume","qvol","confirm"])
    if d.empty:return d
    d=d[d.confirm!="0"].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return d

def ema(s,n): return s.ewm(span=n,adjust=False).mean()

def rsi(s,n=14):
    z=s.diff(); up=z.clip(lower=0); dn=-z.clip(upper=0)
    au=up.ewm(alpha=1/n,min_periods=n,adjust=False).mean()
    ad=dn.ewm(alpha=1/n,min_periods=n,adjust=False).mean()
    rs=au/ad.replace(0,np.nan)
    return (100-100/(1+rs)).fillna(50)

def add(d):
    d=d.copy()
    d["ema20"]=ema(d["close"],20); d["ema50"]=ema(d["close"],50); d["ema200"]=ema(d["close"],200)
    d["rsi"]=rsi(d["close"])
    m=ema(d["close"],12)-ema(d["close"],26); sig=ema(m,9)
    d["macd"]=m; d["signal"]=sig; d["hist"]=m-sig
    pc=d["close"].shift(1)
    tr=pd.concat([d["high"]-d["low"],(d["high"]-pc).abs(),(d["low"]-pc).abs()],axis=1).max(axis=1)
    d["atr"]=tr.ewm(alpha=1/14,min_periods=14,adjust=False).mean()
    d["vma"]=d["volume"].rolling(20).mean()
    d["vr"]=d["volume"]/d["vma"].replace(0,np.nan)
    return d

def trend(d):
    if len(d)<210:return 0
    x=d.iloc[-1]; return (5 if x["close"]>x["ema20"] else 0)+(5 if x["ema20"]>x["ema50"] else 0)+(5 if x["ema50"]>x["ema200"] else 0)

def setup(d):
    if len(d)<60:return None,0
    x,p=d.iloc[-1],d.iloc[-2]
    ph=d["high"].iloc[-21:-1].max()
    if x["close"]>ph or (p["low"]<=ph*1.015 and x["close"]>=ph*.995):
        return "BREAKOUT + RETEST",15
    if abs(x["close"]-x["ema20"])/x["close"]<=.018 or (p["close"]<p["ema20"] and x["close"]>x["ema20"]):
        return "PULLBACK",15
    sup=d["low"].iloc[-31:-1].min()
    if x["low"]<=sup*1.015 and x["close"]>sup*1.01:return "SUPPORT REVERSAL",15
    return None,0

def confirm(d):
    if len(d)<60:return 0,False
    x=d.iloc[-1]; sc=(3 if x["close"]>x["ema20"] else 0)+(3 if x["ema20"]>x["ema50"] else 0)
    sc+=(2 if x["rsi"]>=50 else 0)+(2 if x["hist"]>0 else 0)
    return sc,sc>=6

def signal(c,d,h4,h1,btc):
    if min(len(d),len(h4),len(h1))<60 or len(d)<210 or len(h4)<210:return None
    x=h4.iloc[-1]; y=h1.iloc[-1]; setup_name,sp=setup(h4)
    cp,ok=confirm(h1)
    if not setup_name or not ok:return None
    score=trend(d)+trend(h4)
    score += 10 if x["close"]>x["ema20"]>x["ema50"]>x["ema200"] else (7 if x["close"]>x["ema20"]>x["ema50"] else (4 if x["close"]>x["ema20"] else 0))
    score += sp
    score += 10 if 52<=x["rsi"]<=68 else (6 if 48<=x["rsi"]<52 or 68<x["rsi"]<=74 else (2 if x["rsi"]>74 else 0))
    score += 10 if x["hist"]>0 and x["macd"]>x["signal"] else (6 if x["hist"]>0 else 0)
    vr=x["vr"]
    score += 15 if vr>=2 else (12 if vr>=1.5 else (8 if vr>=1.2 else (5 if vr>=1 else 0)))
    score += cp + btc
    if score<MIN_SCORE or y["close"]>x["ema20"]*1.10:return None
    # --- Structure-based trade levels (keeps V2 signal scoring unchanged) ---
    entry_ref=float(y["close"]); atr=float(x["atr"])
    if not np.isfinite(atr) or atr<=0:return None

    def pivots(frame, left=2, right=2):
        highs=[]; lows=[]
        hi=frame["high"].to_numpy(); lo=frame["low"].to_numpy()
        for i in range(left, len(frame)-right):
            if hi[i] >= hi[i-left:i].max() and hi[i] > hi[i+1:i+right+1].max():
                highs.append((i,float(hi[i])))
            if lo[i] <= lo[i-left:i].min() and lo[i] < lo[i+1:i+right+1].min():
                lows.append((i,float(lo[i])))
        return highs,lows

    def cluster_levels(levels, tol):
        levels=sorted(levels)
        clusters=[]
        for v in levels:
            if not clusters or abs(v-np.mean(clusters[-1])) > tol:
                clusters.append([v])
            else:
                clusters[-1].append(v)
        return [float(np.mean(z)) for z in clusters]

    # Use 4H as the primary structure and 1H for entry refinement.
    ph4, pl4 = pivots(h4.tail(120))
    ph1, pl1 = pivots(h1.tail(160))
    tol4=max(0.006*entry_ref, 0.35*atr)
    tol1=max(0.004*entry_ref, 0.25*atr)

    resistances=cluster_levels([v for _,v in ph4]+[v for _,v in ph1], max(tol4,tol1))
    supports=cluster_levels([v for _,v in pl4]+[v for _,v in pl1], max(tol4,tol1))

    # For a long setup, prefer a nearby support below price as the entry zone.
    support_candidates=[v for v in supports if v < entry_ref]
    if not support_candidates:
        return None
    support=max(support_candidates)

    # Entry is the support/retest area, but never materially above the current 1H price.
    entry=max(support, entry_ref-0.35*atr)
    if entry > entry_ref: entry=entry_ref

    # Stop sits below the structural support with an ATR buffer.
    stop=min(support-0.20*atr, entry-0.80*atr)

    # Reject structurally weak/tight stops.
    risk=entry-stop
    if risk<=0 or risk>0.08*entry:
        return None

    # TP levels are the next meaningful resistances above entry.
    r_levels=sorted([r for r in resistances if r > entry*(1+0.003)])
    valid=[r for r in r_levels if (r-entry)/risk >= 2.0]
    if not valid:
        return None

    tp1=valid[0]
    tp2=valid[1] if len(valid)>1 else tp1+0.5*risk
    tp3=valid[2] if len(valid)>2 else tp2+0.75*risk

    return {"symbol":c["symbol"],"score":int(score),"setup":setup_name,"entry":entry,
            "sl":stop,"tp1":tp1,"tp2":tp2,"tp3":tp3,
            "rsi":float(x["rsi"]),"vr":float(vr) if pd.notna(vr) else 0,
            "support":support,"entry_ref":entry_ref,"rr_tp1":(tp1-entry)/risk}

def fp(v):
    v=float(v)
    if v>=1000:return f"{v:,.2f}"
    if v>=1:return f"{v:,.4f}"
    if v>=.01:return f"{v:.5f}"
    if v>=.0001:return f"{v:.7f}"
    return f"{v:.10f}".rstrip("0").rstrip(".")

def main():
    t=time.time(); print("AKAM V2 starting")
    u=universe(); ins=instruments(); tk=tickers()
    liq=[]
    for c in u:
        if c["symbol"] not in ins:continue
        z=tk.get(c["symbol"])
        if z and z["vol"]>=MIN_VOL:
            q=dict(c); q.update(z); liq.append(q)
    liq.sort(key=lambda x:x["market_cap"],reverse=True); liq=liq[:500]
    print(f"Universe={len(u)} Liquid={len(liq)}")

    b1=add(candles("BTC-USDT","1Dutc",220)); b4=add(candles("BTC-USDT","4H",220))
    btc=(5 if len(b1)>=210 and b1.iloc[-1]["close"]>b1.iloc[-1]["ema200"] else 0)
    btc+=(5 if len(b4)>=210 and b4.iloc[-1]["close"]>b4.iloc[-1]["ema50"] else 0)

    pre=[]
    for i,c in enumerate(liq,1):
        try:
            d=add(candles(c["instId"],"1Dutc",220))
            if len(d)>=210:
                x=d.iloc[-1]
                if x["close"]>x["ema20"] and x["ema20"]>x["ema50"] and x["close"]>x["ema200"]:
                    c["_d"]=d; pre.append(c)
        except Exception as e: print("1D skip",c["symbol"],e)
        if i%50==0: print("1D",i,"/",len(liq),"candidates",len(pre))
        time.sleep(.08)
    pre=sorted(pre,key=lambda x:x["market_cap"],reverse=True)[:DEEP]
    signals=[]
    for i,c in enumerate(pre,1):
        try:
            d=c["_d"]; h4=add(candles(c["instId"],"4H",220)); h1=add(candles(c["instId"],"1H",100))
            z=signal(c,d,h4,h1,btc)
            if z:signals.append(z);print("SIGNAL",z["symbol"],z["score"])
        except Exception as e:print("deep skip",c["symbol"],e)
        if i%10==0:print("Deep",i,"/",len(pre))
        time.sleep(.08)

    signals=sorted(signals,key=lambda x:x["score"],reverse=True)[:5]
    now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg=["🚨 AKAM AI SMART SCANNER V2","",f"🕒 {now}","📡 CoinGecko + OKX Public API",
         f"🔎 Universe: {len(u)} | 💧 Liquid: {len(liq)}",f"🎯 Minimum score: {MIN_SCORE}/110",""]
    if not signals:
        msg+=["❌ سیگنال واجد شرایط پیدا نشد.","","فیلترها: 1D + 4H trend | EMA | Setup | RSI | MACD | Volume | 1H confirmation | BTC regime | No-Chase"]
    else:
        for n,z in enumerate(signals,1):
            msg += [f"🔥 #{n} {z['symbol']}/USDT",f"🏆 Score: {z['score']}/110",f"📌 {z['setup']}",
                    f"🟢 Entry: {fp(z['entry'])}",f"🛑 SL: {fp(z['sl'])}",
                    f"🎯 TP1: {fp(z['tp1'])}",f"🎯 TP2: {fp(z['tp2'])}",f"🎯 TP3: {fp(z['tp3'])}",
                    f"🧱 Support: {fp(z['support'])} | R/R TP1: {z['rr_tp1']:.2f}",
                    f"RSI: {z['rsi']:.1f} | Volume: {z['vr']:.2f}x","⚠️ سطوح Entry/SL/TP بر اساس ساختار بازار؛ مدیریت ریسک ضروری است.","━━━━━━━━━━━━"]
    tg("\n".join(msg)); print("Finished in",round(time.time()-t,1),"sec")

if __name__=="__main__": main()
