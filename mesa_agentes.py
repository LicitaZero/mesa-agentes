#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mesa de Agentes: backtest e paper trading de memecoins da Solana com dados reais.

- Usa só a biblioteca padrão do Python 3.8 ou mais novo (nada para instalar).
- O dinheiro é simulado. Este script nunca envia ordem, nunca pede carteira nem chave.
- Dados: GeckoTerminal (candles e pools em alta) e RugCheck (risco do token).

Comandos:
  python mesa_agentes.py demo                 roda tudo com dados sintéticos (sem internet)
  python mesa_agentes.py descobrir            lista pools em alta na Solana  -> pools.json
  python mesa_agentes.py baixar               baixa candles históricos       -> dados/
  python mesa_agentes.py backtest             simula a estratégia no passado -> relatorio.html
  python mesa_agentes.py tudo                 descobrir + baixar + backtest
  python mesa_agentes.py ao-vivo              paper trading contínuo          -> relatorio_ao_vivo.html
"""
import argparse
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GT = "https://api.geckoterminal.com/api/v2"
RUG = "https://api.rugcheck.xyz/v1"
PAUSA_API = 2.3  # segundos entre chamadas; a API gratuita aceita cerca de 30 por minuto
# pools de SOL e stablecoins não entram: o teste é de memecoins
EXCLUIR_NOMES = {"SOL", "WSOL", "USDC", "USDT", "USDS", "PYUSD", "USDE", "USDX", "USD1", "USDG", "DAI", "USDD", "EURC", "USDH", "UXD"}
EXCLUIR_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

CFG = {
    "capital": 50.0,     # banca fictícia inicial (USD)
    "tamanho": 0.20,     # Sizer: fração do patrimônio por entrada (20% de $50 = $10)
    "fim_data": "2026-12-31",  # último dia do acompanhamento ao vivo (horário de Brasília)
    "slip": 0.015,       # Executor: derrapagem em cada lado (1,5%)
    "taxa": 0.0025,      # Executor: taxa de swap em cada lado
    "stop": 0.12,        # Stop Guard: perda máxima em relação à entrada
    "tp1": 0.30,         # TP Ladder: vende metade com +30%
    "tp2": 0.60,         # TP Ladder: vende o resto com +60%
    "max_pos": 5,        # posições abertas ao mesmo tempo
    "espera": 5,         # candles de pausa depois de uma saída
    "vol_mult": 3.0,     # sinal de volume: volume >= 3x a mediana recente
    "jan_vol": 30,       # janela da mediana de volume
    "jan_brk": 20,       # janela do rompimento
    "min_hist": 40,      # candles mínimos antes de operar uma moeda
    "min_sinais": 2,     # de 3 sinais (volume, rompimento, pressão)
    "vol_med_min": 100.0,  # volume mediano mínimo por candle (USD): ignora pool morta
    "max_candles": 288,  # saída por tempo (288 candles de 5 min = 24 h)
    "agg": 5,            # minutos por candle
}


# ---------------------------------------------------------------- utilidades
def agora():
    return int(time.time())


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


BRT = timezone(timedelta(hours=-3))


def ts_fim(dia):
    """Último segundo do dia (AAAA-MM-DD) no horário de Brasília, em epoch UTC."""
    d = datetime.strptime(dia, "%Y-%m-%d").replace(tzinfo=BRT) + timedelta(days=1)
    return int(d.timestamp()) - 1


def fmt_dia(ts):
    return datetime.fromtimestamp(ts, BRT).strftime("%d/%m")


def http_get(url, tentativas=4):
    ultimo = None
    for i in range(tentativas):
        try:
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json;version=20230302", "User-Agent": "mesa-agentes/1.0"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            ultimo = "HTTP %s" % e.code
            if e.code == 429:
                time.sleep(12 * (i + 1))
            elif e.code in (400, 404):
                break
            else:
                time.sleep(3 * (i + 1))
        except Exception as e:  # rede, JSON inválido etc.
            ultimo = str(e)
            time.sleep(3 * (i + 1))
    raise RuntimeError("falha ao chamar %s (%s)" % (url, ultimo))


def ler_json(caminho, padrao):
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return padrao


def gravar_json(caminho, obj):
    tmp = str(caminho) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, caminho)


# ---------------------------------------------------------------- dados reais
def cmd_descobrir(a):
    achados = {}
    rotas = ["trending_pools", "pools?sort=h24_volume_usd_desc"]
    for rota in rotas:
        for pagina in range(1, a.paginas + 1):
            sep = "&" if "?" in rota else "?"
            url = "%s/networks/solana/%s%spage=%d" % (GT, rota, sep, pagina)
            try:
                j = http_get(url)
            except Exception as e:
                print("aviso:", e)
                break
            for it in j.get("data", []):
                at = it.get("attributes", {})
                addr = at.get("address")
                if not addr:
                    continue
                liq = float(at.get("reserve_in_usd") or 0)
                vol24 = float((at.get("volume_usd") or {}).get("h24") or 0)
                criado = at.get("pool_created_at")
                idade_h = None
                if criado:
                    try:
                        dt = datetime.fromisoformat(criado.replace("Z", "+00:00"))
                        idade_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                    except Exception:
                        pass
                base = (((it.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                mint = base.split("_", 1)[1] if "_" in base else base
                nome = (at.get("name") or "?").split(" / ")[0].strip()
                achados[addr] = {"addr": addr, "nome": nome, "mint": mint, "liq": liq, "vol24": vol24, "idade_h": idade_h}
            time.sleep(PAUSA_API)
    lista = [
        p for p in achados.values()
        if p["liq"] >= a.min_liq and (p["idade_h"] is None or p["idade_h"] >= a.min_idade_h)
        and p["vol24"] >= a.min_vol24 and p["mint"]
        and p["nome"].upper() not in EXCLUIR_NOMES and p["mint"] not in EXCLUIR_MINTS
    ]
    lista.sort(key=lambda p: -p["vol24"])
    por_token = {}
    for pool in lista:
        por_token.setdefault(pool["mint"], pool)
    lista = list(por_token.values())[:a.top]
    usados = {}
    for p in lista:  # nomes únicos
        n = usados.get(p["nome"], 0)
        usados[p["nome"]] = n + 1
        if n:
            p["nome"] = "%s#%d" % (p["nome"], n + 1)
    gravar_json("pools.json", lista)
    print("%d pools salvos em pools.json" % len(lista))
    for p in lista:
        print("  %-14s liq $%-12s vol24h $%s" % (p["nome"], format(int(p["liq"]), ","), format(int(p["vol24"]), ",")))
    if not lista:
        print("Nenhum pool passou nos filtros. Tente --min-liq e --min-idade-h menores.")


def baixar_candles(addr, paginas, agg):
    candles = {}
    antes = None
    for _ in range(paginas):
        url = "%s/networks/solana/pools/%s/ohlcv/minute?aggregate=%d&limit=1000&currency=usd" % (GT, addr, agg)
        if antes:
            url += "&before_timestamp=%d" % antes
        j = http_get(url)
        lst = (((j.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        if not lst:
            break
        for r in lst:
            candles[int(r[0])] = [int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
        antes = min(int(r[0]) for r in lst)
        time.sleep(PAUSA_API)
        if len(lst) < 1000:
            break
    return candles


def salvar_pool(pasta, pool, novos, cutoff=None):
    pasta.mkdir(exist_ok=True)
    arq = pasta / ("%s.json" % pool["addr"])
    atual = ler_json(arq, {"meta": pool, "candles": []})
    mapa = {c[0]: c for c in atual["candles"]}
    mapa.update(novos)
    candles = [mapa[t] for t in sorted(mapa)]
    if cutoff:  # descarta o que é antigo demais, para o estado não crescer sem limite
        candles = [c for c in candles if c[0] >= cutoff]
    gravar_json(arq, {"meta": pool, "candles": candles})
    return len(candles)


def cmd_baixar(a):
    pools = ler_json("pools.json", [])
    if not pools:
        sys.exit("pools.json não existe ou está vazio. Rode primeiro: python mesa_agentes.py descobrir")
    pasta = Path("dados")
    for pool in pools:
        try:
            novos = baixar_candles(pool["addr"], a.paginas, a.agg)
            n = salvar_pool(pasta, pool, novos)
            print("%-14s %d candles de %d min" % (pool["nome"], n, a.agg))
        except Exception as e:
            print("%-14s ERRO: %s" % (pool["nome"], e))


def carregar_moedas(pasta):
    moedas = []
    for arq in sorted(Path(pasta).glob("*.json")):
        j = ler_json(arq, None)
        if not j or not j.get("candles"):
            continue
        c = [r for r in j["candles"] if r[1] > 0 and r[2] > 0 and r[3] > 0 and r[4] > 0]
        c.sort(key=lambda r: r[0])
        meta = j.get("meta", {})
        moedas.append({"nome": meta.get("nome", arq.stem), "addr": meta.get("addr", arq.stem),
                       "mint": meta.get("mint", ""), "c": c, "rug_ok": True})
    return moedas


def checar_rug(mint, cache):
    ent = cache.get(mint)
    if ent and agora() - ent["t"] < 3600:
        return ent["ok"]
    try:
        j = http_get("%s/tokens/%s/report/summary" % (RUG, mint), tentativas=2)
        perigos = [r.get("name") for r in (j.get("risks") or []) if str(r.get("level", "")).lower() == "danger"]
        cache[mint] = {"t": agora(), "ok": len(perigos) == 0, "perigos": perigos,
                       "score": j.get("score_normalised", j.get("score"))}
    except Exception as e:
        print("aviso: RugCheck sem resposta para %s (%s); moeda bloqueada por segurança" % (mint[:6], e))
        cache[mint] = {"t": agora() - 3000, "ok": False, "perigos": ["sem resposta"], "score": None}
    time.sleep(PAUSA_API)
    return cache[mint]["ok"]


# ---------------------------------------------------------------- agentes e simulação
def sinais(c, cfg):
    """Para cada candle i devolve (quantidade, nomes) usando só dados até o fechamento de i."""
    n = len(c)
    saida = [None] * n
    vols = [r[5] for r in c]
    for i in range(n):
        if i < cfg["min_hist"]:
            saida[i] = (0, [])
            continue
        _, o, h, l, cl, v = c[i]
        med = statistics.median(vols[i - cfg["jan_vol"]:i])
        s = []
        if med >= cfg["vol_med_min"] and v >= cfg["vol_mult"] * med:
            s.append("volume")
        if cl > max(r[2] for r in c[i - cfg["jan_brk"]:i]):
            s.append("rompimento")
        ult = c[i - 3:i + 1]
        verdes = sum(1 for r in ult if r[4] > r[1])
        pos = (cl - l) / (h - l) if h > l else 0.5
        if cl > o and verdes >= 3 and pos >= 0.6:
            s.append("pressão")
        saida[i] = (len(s), s)
    return saida


def simular(moedas, cfg, inicio_ts=None, fechar_fim=True):
    moedas = [m for m in moedas if len(m["c"]) > cfg["min_hist"] + 2]
    por_ts = {}
    for k, m in enumerate(moedas):
        m["_sig"] = sinais(m["c"], cfg)
        for i, r in enumerate(m["c"]):
            por_ts.setdefault(r[0], []).append((k, i))

    estado = {"cash": cfg["capital"]}
    pos, pend, cool, ultimo = {}, {}, {}, {}
    trades, marks, curva = [], [], []

    def valor(p):
        return p["custo"] * p["frac"] * ultimo[p["k"]] / p["entrada"]

    def vender(p, frac, px, ts, i, motivo):
        liq = px * (1 - cfg["slip"]) * (1 - cfg["taxa"])
        rec = p["custo"] * frac * liq / p["entrada"]
        estado["cash"] += rec
        p["recebido"] += rec
        p["frac"] -= frac
        p["saidas"].append({"ts": ts, "px": px, "frac": frac, "motivo": motivo})
        tipo = "stop" if motivo.startswith("stop") else ("tp" if motivo.startswith("TP") else "saida")
        marks.append({"m": moedas[p["k"]]["nome"], "ts": ts, "tipo": tipo, "px": px, "motivo": motivo, "fracao": frac, "valor_liquido": rec})
        if p["frac"] <= 1e-9:
            trades.append({
                "m": moedas[p["k"]]["nome"], "ts0": p["ts0"], "px0": p["ref"], "ts1": ts, "px1": px,
                "motivos": [s["motivo"] for s in p["saidas"]], "sinais": p["sinais"],
                "custo": p["custo"], "pnl": p["recebido"] - p["custo"],
                "pct": (p["recebido"] / p["custo"] - 1) * 100, "barras": i - p["i0"],
            })
            del pos[p["k"]]
            cool[p["k"]] = i + cfg["espera"]

    def gerenciar(p, i, r):
        ts, o, h, l, cl = r[0], r[1], r[2], r[3], r[4]
        if l <= p["stop"] or o <= p["stop"]:
            vender(p, p["frac"], min(o, p["stop"]), ts, i, "stop (empate)" if p["tp1"] else "stop")
            return
        if not p["tp1"] and h >= p["ref"] * (1 + cfg["tp1"]):
            p["tp1"] = True
            p["stop"] = p["ref"]
            vender(p, 0.5, max(o, p["ref"] * (1 + cfg["tp1"])), ts, i, "TP1")
        if p["tp1"] and h >= p["ref"] * (1 + cfg["tp2"]):
            vender(p, p["frac"], max(o, p["ref"] * (1 + cfg["tp2"])), ts, i, "TP2")
            return
        if i - p["i0"] >= cfg["max_candles"]:
            vender(p, p["frac"], cl, ts, i, "tempo")

    ultimo_ts = None
    for ts in sorted(por_ts):
        ultimo_ts = ts
        rodadas = sorted(por_ts[ts])
        for k, i in rodadas:
            ultimo[k] = moedas[k]["c"][i][1]
        for k, i in rodadas:
            m = moedas[k]
            r = m["c"][i]
            p = pos.get(k)
            if k in pend and p is None:
                pd = pend.pop(k)
                if ts - pd["ts"] <= 3 * cfg["agg"] * 60 and len(pos) < cfg["max_pos"]:
                    eq = estado["cash"] + sum(valor(q) for q in pos.values())
                    custo = min(eq * cfg["tamanho"], estado["cash"])
                    if custo > 0.01:
                        estado["cash"] -= custo
                        ref = r[1]
                        p = pos[k] = {
                            "k": k, "ref": ref, "entrada": ref * (1 + cfg["slip"]) * (1 + cfg["taxa"]),
                            "custo": custo, "frac": 1.0, "tp1": False, "stop": ref * (1 - cfg["stop"]),
                            "i0": i, "ts0": ts, "saidas": [], "recebido": 0.0, "sinais": pd["sig"],
                        }
                        marks.append({"m": m["nome"], "ts": ts, "tipo": "compra", "px": ref, "custo": custo})
            if p is not None and k in pos:
                gerenciar(p, i, r)
            rug_por_candle = m.get("rug_por_candle")
            rug_ok = m["rug_ok"] if rug_por_candle is None else rug_por_candle.get(str(ts), False)
            if (k not in pos and k not in pend and i >= cool.get(k, -1) and rug_ok
                    and (inicio_ts is None or ts >= inicio_ts)):
                n, nomes = m["_sig"][i]
                if n >= cfg["min_sinais"]:
                    pend[k] = {"ts": ts, "sig": nomes}
        for k, i in rodadas:
            ultimo[k] = moedas[k]["c"][i][4]
        eq = estado["cash"] + sum(valor(q) for q in pos.values())
        curva.append([ts, round(eq, 4)])

    abertas = []
    for k, p in list(pos.items()):
        if fechar_fim:
            vender(p, p["frac"], ultimo[k], ultimo_ts, len(moedas[k]["c"]) - 1, "fim")
        else:
            abertas.append({"m": moedas[k]["nome"], "ts0": p["ts0"], "px0": p["ref"], "px": ultimo[k],
                            "pct": (ultimo[k] / p["entrada"] - 1) * 100, "stop": p["stop"], "tp1": p["tp1"]})
    if curva:
        # patrimônio final já depois dos custos de saída (fechar_fim) ou marcado a mercado (ao vivo)
        curva[-1][1] = round(estado["cash"] + sum(valor(q) for q in pos.values()), 4)
    pendentes = [{"m": moedas[k]["nome"], "ts": v["ts"], "sinais": v["sig"]} for k, v in pend.items()]
    return {"trades": trades, "marks": marks, "curva": curva, "abertas": abertas, "pendentes": pendentes,
            "moedas": moedas}


def estatisticas(res, cfg):
    t = res["trades"]
    curva = res["curva"]
    n = len(t)
    ganhos = [x["pnl"] for x in t if x["pnl"] > 0]
    perdas = [x["pnl"] for x in t if x["pnl"] <= 0]
    eq_final = curva[-1][1] if curva else cfg["capital"]
    pico, dd = 0.0, 0.0
    for _, e in curva:
        pico = max(pico, e)
        if pico > 0:
            dd = max(dd, (pico - e) / pico)
    bh = []
    for m in res["moedas"]:
        c = m["c"]
        if len(c) > cfg["min_hist"] + 2:
            bh.append(c[-1][4] / c[cfg["min_hist"]][4] - 1)
    corte = None
    partes = {}
    if curva:
        t0, t1 = curva[0][0], curva[-1][0]
        corte = t0 + 0.7 * (t1 - t0)
        for nome, sel in (("amostra1", [x for x in t if x["ts0"] < corte]), ("amostra2", [x for x in t if x["ts0"] >= corte])):
            partes[nome] = {
                "n": len(sel),
                "acerto": (sum(1 for x in sel if x["pnl"] > 0) / len(sel) * 100) if sel else None,
                "pnl": sum(x["pnl"] for x in sel),
            }
    return {
        "eq_final": eq_final,
        "retorno_pct": (eq_final / cfg["capital"] - 1) * 100,
        "n": n,
        "acerto": (len(ganhos) / n * 100) if n else None,
        "fator": (sum(ganhos) / -sum(perdas)) if perdas and sum(perdas) < 0 else None,
        "dd": dd * 100,
        "media_ganho": statistics.mean(ganhos) if ganhos else None,
        "media_perda": statistics.mean(perdas) if perdas else None,
        "bh": (statistics.mean(bh) * 100) if bh else None,
        "corte": corte,
        "partes": partes,
        "ini": curva[0][0] if curva else None,
        "fim": curva[-1][0] if curva else None,
    }


def cfg_dos_args(a):
    cfg = dict(CFG)
    mapa = {"capital": "capital", "tamanho": "tamanho", "stop": "stop", "tp1": "tp1", "tp2": "tp2",
            "slip": "slip", "max_pos": "max_pos", "min_sinais": "min_sinais", "agg": "agg",
            "ate": "fim_data"}
    for arg, chave in mapa.items():
        v = getattr(a, arg, None)
        if v is not None:
            cfg[chave] = v
    return cfg


def resumo_terminal(st, cfg):
    print()
    print("Período: %s até %s" % (fmt_ts(st["ini"]) if st["ini"] else "-", fmt_ts(st["fim"]) if st["fim"] else "-"))
    print("Patrimônio: $%.2f (%+.2f%%) | operações: %d | acerto: %s | fator de lucro: %s | drawdown máx: %.1f%%" % (
        st["eq_final"], st["retorno_pct"], st["n"],
        ("%.0f%%" % st["acerto"]) if st["acerto"] is not None else "-",
        ("%.2f" % st["fator"]) if st["fator"] is not None else "-", st["dd"]))
    if st["bh"] is not None:
        print("Comprar e segurar (média simples das moedas): %+.1f%%" % st["bh"])
    for nome, rot in (("amostra1", "Primeiros 70% do período"), ("amostra2", "Últimos 30% do período")):
        p = st["partes"].get(nome)
        if p:
            print("  %s: %d operações, resultado $%.2f" % (rot, p["n"], p["pnl"]))


# ---------------------------------------------------------------- relatório HTML
def _preco(p):
    return ("%.10f" % p).rstrip("0").rstrip(".")


def agentes_moeda(m, cfg, res, st):
    """Parecer de cada agente e veredito do cérebro para a moeda, no último candle."""
    c = m["c"]
    i = len(c) - 1
    n, nomes = m["_sig"][i]
    med = statistics.median([r[5] for r in c[i - cfg["jan_vol"]:i]])
    razao = c[i][5] / med if med > 0 else 0.0
    brk = (c[i][4] / max(r[2] for r in c[i - cfg["jan_brk"]:i]) - 1) * 100
    verdes = sum(1 for r in c[i - 3:i + 1] if r[4] > r[1])
    aberta = next((x for x in res["abertas"] if x["m"] == m["nome"]), None)
    pend = any(x["m"] == m["nome"] for x in res["pendentes"])
    checado = m.get("rug_checado", False)
    A = []
    if checado:
        A.append({"n": "Rug Gate", "s": "ok" if m["rug_ok"] else "bad", "t": "PASS" if m["rug_ok"] else "BLOQUEADA"})
    else:
        A.append({"n": "Rug Gate", "s": "idle", "t": "não verificado (só ao vivo)"})
    A.append({"n": "Volume", "s": "ok" if "volume" in nomes else ("warn" if razao >= 2 else "bad"), "t": "%.1fx a mediana" % razao})
    A.append({"n": "Rompimento", "s": "ok" if "rompimento" in nomes else "bad", "t": "%+.1f%% sobre a máxima" % brk})
    A.append({"n": "Pressão", "s": "ok" if "pressão" in nomes else ("warn" if verdes >= 2 else "bad"), "t": "%d de 4 candles verdes" % verdes})
    A.append({"n": "Sizer", "s": "ok", "t": "%.0f%% · $%.2f" % (cfg["tamanho"] * 100, st["eq_final"] * cfg["tamanho"])})
    A.append({"n": "Executor", "s": "ok", "t": "slip %.1f%% · taxa %.2f%%" % (cfg["slip"] * 100, cfg["taxa"] * 100)})
    A.append({"n": "Stop Guard", "s": "ok" if aberta else "idle", "t": ("stop " + _preco(aberta["stop"])) if aberta else "-%.0f%% da entrada" % (cfg["stop"] * 100)})
    A.append({"n": "TP Ladder", "s": "ok" if aberta else "idle", "t": "TP1 +%.0f%% · TP2 +%.0f%%" % (cfg["tp1"] * 100, cfg["tp2"] * 100)})
    if aberta:
        ver = ("Em posição", "Stop e take profit em vigilância.", False)
    elif pend:
        ver = ("Ordem pendente", "Executa na abertura do próximo candle.", True)
    elif checado and not m["rug_ok"]:
        ver = ("Bloqueado", "Falhou no Rug Gate.", False)
    elif n >= cfg["min_sinais"]:
        ver = ("Sinal ativo", "%d de 3 sinais positivos no último candle." % n, True)
    else:
        ver = ("Observar", "%d de 3 sinais positivos; precisa de %d." % (n, cfg["min_sinais"]), False)
    return {"agentes": A, "veredito": {"r": ver[0], "p": ver[1], "go": ver[2]}}


def separar_viva(moedas, agg):
    """Tira da simulação o candle ainda em formação (senão sinais aparecem e somem quando ele fecha)
    e guarda esse candle só para o gráfico."""
    limite = agora() - agg * 60
    for m in moedas:
        m["c_viva"] = None
        if m["c"] and m["c"][-1][0] > limite:
            m["c_viva"] = m["c"].pop()


def _candles_grafico(m, max_candles):
    c = m["c"] + ([m["c_viva"]] if m.get("c_viva") else [])
    return c[-max_candles:] if max_candles else c


def gerar_relatorio(res, st, cfg, caminho, modo, avisos, json_path=None, max_candles=None):
    passo = max(1, math.ceil(len(res["curva"]) / 600))
    curva = res["curva"][::passo]
    if res["curva"] and curva[-1] != res["curva"][-1]:
        curva.append(res["curva"][-1])
    dados = {
        "modo": modo, "gerado": fmt_ts(agora()), "cfg": cfg, "stats": st, "avisos": avisos,
        "moedas": [dict({"nome": m["nome"], "c": _candles_grafico(m, max_candles)}, **agentes_moeda(m, cfg, res, st))
                   for m in res["moedas"]],
        "trades": res["trades"], "marks": res["marks"], "curva": curva,
        "abertas": res["abertas"], "pendentes": res["pendentes"],
    }
    js = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    if caminho:
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(MODELO.replace("__DADOS__", js))
        print("Relatório salvo em %s" % caminho)
    if json_path:
        tmp = str(json_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(dados, ensure_ascii=False, separators=(",", ":")))
        os.replace(tmp, json_path)


MODELO = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mesa de Agentes - Relatório</title>
<style>
:root{--bg:#110d0e;--surface:#1a1315;--surface2:#231a1d;--line:#3a2a2f;--fg:#f2e9e6;--muted:#a3908c;--accent:#ff7a45;--good:#4ade80;--bad:#ff5470;--warn:#f5c542;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;padding:16px max(16px,env(safe-area-inset-right)) 40px max(16px,env(safe-area-inset-left))}
h1{font-size:1.4rem;margin:0}h2{font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:500;margin:0 0 8px}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.top{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin-bottom:14px}
.badge{font-size:.7rem;padding:3px 8px;border:1px solid var(--warn);color:var(--warn);border-radius:4px;letter-spacing:.04em}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:14px;min-width:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.kpi .l{font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}.kpi .v{font-size:1.05rem}
.up{color:var(--good)}.down{color:var(--bad)}.mute{color:var(--muted)}
canvas{width:100%;display:block}
#eq{height:120px}#cv{height:340px}
.tabs{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px}
.tab{flex:0 0 auto;background:var(--surface2);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit}
.tab[aria-pressed=true]{border-color:var(--accent)}
.ctl{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:8px 0}
select,input[type=range]{background:var(--surface2);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font:inherit}
input[type=range]{flex:1;min-width:140px;padding:0}
.wrapt{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.8rem}
th{text-align:left;color:var(--muted);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}tr.lk{cursor:pointer}tr.lk:hover{background:var(--surface2)}
ul{margin:0;padding-left:18px;color:var(--muted)}li{margin:3px 0}
.leg{display:flex;flex-wrap:wrap;gap:12px;font-size:.78rem;color:var(--muted)}
.hub{display:flex;align-items:center;gap:16px;background:var(--surface2);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0}
.ring{flex:0 0 auto;width:60px;height:60px;border-radius:50%;border:2px solid var(--accent);display:grid;place-items:center;box-shadow:0 0 0 5px var(--surface),0 0 18px rgba(255,122,69,.35);font:.62rem/1.2 ui-monospace,monospace;text-align:center}
.verdict{font-size:1.1rem;font-weight:700}
.agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-bottom:10px}
.agent{background:var(--surface2);border:1px solid var(--line);border-left:3px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0}
.agent.ok{border-left-color:var(--good)}.agent.warn{border-left-color:var(--warn)}.agent.bad{border-left-color:var(--bad)}
.agent .n{font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}.agent .t{font-size:.78rem;margin-top:2px;overflow-wrap:anywhere}
</style></head><body>
<div class="top"><h1>Mesa de Agentes</h1><span class="badge" id="modo"></span><span class="mute mono" id="gerado"></span></div>
<div class="panel"><div class="kpis" id="kpis"></div></div>
<div class="panel"><h2>Patrimônio simulado</h2><canvas id="eq"></canvas>
<div class="mute" id="split" style="margin-top:8px"></div></div>
<div class="panel"><h2>Entradas e saídas por moeda</h2><div class="tabs" id="tabs"></div>
<div class="hub" id="hub"></div><div class="agents" id="agents"></div>
<div class="ctl"><label>Janela <select id="zoom"><option value="100">100 candles</option><option value="250" selected>250 candles</option><option value="600">600 candles</option><option value="0">tudo</option></select></label>
<input id="pos" type="range" min="0" max="1000" value="1000" aria-label="Posição no tempo"></div>
<canvas id="cv"></canvas>
<div class="leg"><span><span style="color:var(--accent)">&#9650;</span> compra</span><span><span class="up">&#9660;</span> venda em TP</span><span><span class="down">&#9660;</span> stop</span><span><span class="mute">&#9660;</span> saída por tempo ou fim</span></div></div>
<div class="panel"><h2>Posições abertas e ordens pendentes</h2><div id="abertas"></div></div>
<div class="panel"><h2>Operações (toque numa linha para ver no gráfico)</h2><div class="wrapt"><table id="tb"></table></div></div>
<div class="panel"><h2>Leia antes de confiar nestes números</h2><ul id="avisos"></ul></div>
<script>
const D=__DADOS__;
const $=s=>document.querySelector(s);
const fm=(n,d=2)=>n.toLocaleString('pt-BR',{minimumFractionDigits:d,maximumFractionDigits:d});
function fp(v){if(!isFinite(v)||v<=0)return'0';const d=Math.min(12,Math.max(2,3-Math.floor(Math.log10(v))));return v.toFixed(d)}
const dt=ts=>new Date(ts*1000).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
const cs=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let cur=0,zoom=250,start=0;
function idxOf(c,ts){let lo=0,hi=c.length-1;while(lo<hi){const m=(lo+hi+1)>>1;if(c[m][0]<=ts)lo=m;else hi=m-1}return lo}
function kpi(l,v,c){return '<div class="kpi"><div class="l">'+l+'</div><div class="v mono '+(c||'')+'">'+v+'</div></div>'}
function head(){
 const s=D.stats;$('#modo').textContent=D.modo;$('#gerado').textContent='gerado '+D.gerado+' UTC';
 $('#kpis').innerHTML=kpi('Patrimônio','$'+fm(s.eq_final))+kpi('Resultado',(s.retorno_pct>=0?'+':'')+fm(s.retorno_pct)+'%',s.retorno_pct>=0?'up':'down')
 +kpi('Operações',s.n)+kpi('Acerto',s.acerto==null?'-':fm(s.acerto,0)+'%')+kpi('Fator de lucro',s.fator==null?'-':fm(s.fator))
 +kpi('Queda máxima',fm(s.dd,1)+'%','down')+kpi('Ganho médio',s.media_ganho==null?'-':'$'+fm(s.media_ganho))+kpi('Perda média',s.media_perda==null?'-':'$'+fm(s.media_perda))
 +kpi('Comprar e segurar',s.bh==null?'-':(s.bh>=0?'+':'')+fm(s.bh,1)+'%');
 const a=s.partes.amostra1,b=s.partes.amostra2;
 $('#split').textContent=(a&&b)?('Primeiros 70% do período: '+esc(a.n)+' operações, $'+fm(a.pnl)+'. Últimos 30%: '+b.n+' operações, $'+fm(b.pnl)+'. Se os dois lados discordam muito, desconfie.'):'';
 $('#avisos').innerHTML=D.avisos.map(x=>'<li>'+esc(x)+'</li>').join('');
}
function drawEq(){
 const cv=$('#eq'),dpr=devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;if(!W||!D.curva.length)return;
 cv.width=W*dpr;cv.height=H*dpr;const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
 const v=D.curva.map(x=>x[1]),lo=Math.min(...v,D.cfg.capital),hi=Math.max(...v,D.cfg.capital),r=(hi-lo)||1;
 const t0=D.curva[0][0],t1=D.curva[D.curva.length-1][0]||t0+1,X=t=>(t-t0)/((t1-t0)||1)*(W-60),Y=e=>8+(hi-e)/r*(H-16);
 g.strokeStyle=cs('--line');g.setLineDash([4,4]);g.beginPath();g.moveTo(0,Y(D.cfg.capital));g.lineTo(W-60,Y(D.cfg.capital));g.stroke();g.setLineDash([]);
 g.strokeStyle=v[v.length-1]>=D.cfg.capital?cs('--good'):cs('--bad');g.lineWidth=1.6;g.beginPath();
 D.curva.forEach((p,i)=>{const x=X(p[0]),y=Y(p[1]);i?g.lineTo(x,y):g.moveTo(x,y)});g.stroke();
 g.fillStyle=cs('--muted');g.font='11px ui-monospace,monospace';g.textBaseline='middle';
 g.fillText('$'+fm(hi,2),W-56,Y(hi)+4);g.fillText('$'+fm(lo,2),W-56,Y(lo)-4);
}
function tabs(){
 $('#tabs').innerHTML=D.moedas.map((m,i)=>{const n=D.trades.filter(t=>t.m===m.nome).length;return '<button class="tab" data-i="'+i+'" aria-pressed="'+(i===cur)+'">'+esc(m.nome)+' <span class="mute mono">'+n+'</span></button>'}).join('');
 agentes();
}
function agentes(){
 const m=D.moedas[cur],v=m.veredito;
 $('#hub').innerHTML='<div class="ring" style="'+(v.go?'border-color:var(--good)':'')+'">CÉREBRO<br>regras</div><div><div class="mute mono">'+esc(m.nome)+' · último candle</div><div class="verdict" style="color:'+(v.go?'var(--good)':v.r==='Bloqueado'?'var(--bad)':'var(--fg)')+'">'+esc(v.r)+'</div><div class="mute">'+esc(v.p)+'</div></div>';
 $('#agents').innerHTML=m.agentes.map(a=>'<div class="agent '+a.s+'"><div class="n">'+esc(a.n)+'</div><div class="t mono">'+esc(a.t)+'</div></div>').join('');
}
function view(){const n=D.moedas[cur].c.length;const vis=zoom===0?n:Math.min(zoom,n);return{n,vis,max:Math.max(0,n-vis)}}
function drawCv(){
 const m=D.moedas[cur],c=m.c,cv=$('#cv'),dpr=devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;if(!W)return;
 cv.width=W*dpr;cv.height=H*dpr;const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
 const V=view();start=Math.max(0,Math.min(start,V.max));const a=start,b=a+V.vis,data=c.slice(a,b);if(!data.length)return;
 const up=cs('--good'),dn=cs('--bad'),mu=cs('--muted'),ln=cs('--line'),ac=cs('--accent');
 let lo=Math.min(...data.map(d=>d[3])),hi=Math.max(...data.map(d=>d[2]));const pad=(hi-lo)*.08||hi*.05;lo-=pad;hi+=pad;
 const AX=74,VH=44,pw=W-AX,ph=H-VH-16,y=v=>8+(hi-v)/(hi-lo)*ph,step=pw/data.length,bw=Math.max(1,Math.min(10,step*.7));
 g.font='11px ui-monospace,monospace';g.textBaseline='middle';
 for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4,yy=y(v);g.strokeStyle=ln;g.lineWidth=1;g.beginPath();g.moveTo(0,yy);g.lineTo(pw,yy);g.stroke();g.fillStyle=mu;g.textAlign='left';g.fillText(fp(v),pw+6,yy)}
 const vm=Math.max(...data.map(d=>d[5]))||1;
 data.forEach((d,i)=>{const x=i*step+step/2,col=d[4]>=d[1]?up:dn;g.strokeStyle=col;g.fillStyle=col;g.lineWidth=1;
  g.beginPath();g.moveTo(x,y(d[2]));g.lineTo(x,y(d[3]));g.stroke();
  g.fillRect(x-bw/2,y(Math.max(d[1],d[4])),bw,Math.max(1,Math.abs(y(d[1])-y(d[4]))));
  g.globalAlpha=.35;const vh=d[5]/vm*(VH-6);g.fillRect(x-bw/2,H-vh,bw,vh);g.globalAlpha=1});
 const xo=ts=>{if(ts<c[0][0])return null;const i=idxOf(c,ts)-a;return i<0||i>=data.length?null:i*step+step/2};
 D.trades.filter(t=>t.m===m.nome).forEach(t=>{const x0=xo(t.ts0),x1=xo(t.ts1);if(x0==null&&x1==null)return;
  g.strokeStyle=t.pnl>=0?up:dn;g.globalAlpha=.7;g.setLineDash([4,3]);g.beginPath();g.moveTo(x0==null?0:x0,y(t.px0));g.lineTo(x1==null?pw:x1,y(t.px1));g.stroke();g.setLineDash([]);g.globalAlpha=1});
 D.marks.filter(k=>k.m===m.nome).forEach(k=>{const x=xo(k.ts);if(x==null)return;const yy=y(k.px),s=6;
  g.fillStyle=k.tipo==='compra'?ac:k.tipo==='tp'?up:k.tipo==='stop'?dn:mu;g.beginPath();
  if(k.tipo==='compra'){g.moveTo(x,yy-2);g.lineTo(x-s,yy+s+4);g.lineTo(x+s,yy+s+4)}else{g.moveTo(x,yy+2);g.lineTo(x-s,yy-s-4);g.lineTo(x+s,yy-s-4)}g.closePath();g.fill()});
 g.fillStyle=mu;g.textAlign='left';g.fillText(dt(data[0][0]),4,H-VH-2);g.textAlign='right';g.fillText(dt(data[data.length-1][0]),pw-4,H-VH-2);
 $('#pos').value=V.max?Math.round(start/V.max*1000):1000;
}
function esc(v){return String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function trades(){
 const rows=D.trades.slice().sort((x,y)=>y.ts0-x.ts0).map(t=>'<tr class="lk" data-m="'+esc(t.m)+'" data-t="'+t.ts0+'"><td>'+esc(t.m)+'</td><td class="mono">'+dt(t.ts0)+'</td><td class="mono">'+dt(t.ts1)+'</td><td>'+esc(t.motivos.join(' + '))+'</td><td>'+esc(t.sinais.join(', '))+'</td><td class="mono '+(t.pnl>=0?'up':'down')+'">'+(t.pct>=0?'+':'')+fm(t.pct,1)+'%</td><td class="mono '+(t.pnl>=0?'up':'down')+'">'+(t.pnl>=0?'+':'')+'$'+fm(t.pnl)+'</td></tr>').join('');
 $('#tb').innerHTML='<tr><th>Moeda</th><th>Entrada</th><th>Saída</th><th>Motivo</th><th>Sinais</th><th>%</th><th>$</th></tr>'+(rows||'<tr><td colspan="7" class="mute">Nenhuma operação fechada neste período.</td></tr>');
 const ab=D.abertas.map(x=>'<div class="mono">'+esc(x.m)+' · entrada '+fp(x.px0)+' · agora '+fp(x.px)+' · <span class="'+(x.pct>=0?'up':'down')+'">'+(x.pct>=0?'+':'')+fm(x.pct,1)+'%</span></div>').join('')
  +D.pendentes.map(x=>'<div class="mono mute">'+esc(x.m)+' · entrada simulada pendente ('+esc(x.sinais.join(', '))+'), executa na abertura do próximo candle</div>').join('');
 $('#abertas').innerHTML=ab||'<span class="mute">Nada aberto neste momento.</span>';
}
function focus(nome,ts){const i=D.moedas.findIndex(m=>m.nome===nome);if(i<0)return;cur=i;zoom=250;$('#zoom').value='250';
 const c=D.moedas[cur].c;start=Math.max(0,idxOf(c,ts)-90);tabs();drawCv();$('#cv').scrollIntoView({behavior:'smooth',block:'center'})}
$('#tabs').addEventListener('click',e=>{const b=e.target.closest('.tab');if(!b)return;cur=+b.dataset.i;const t=D.trades.find(x=>x.m===D.moedas[cur].nome);
 const c=D.moedas[cur].c;start=t?Math.max(0,idxOf(c,t.ts0)-90):Math.max(0,c.length-zoom);tabs();drawCv()});
$('#tb').addEventListener('click',e=>{const r=e.target.closest('tr.lk');if(r)focus(r.dataset.m,+r.dataset.t)});
$('#zoom').addEventListener('change',e=>{zoom=+e.target.value;drawCv()});
$('#pos').addEventListener('input',e=>{start=Math.round(+e.target.value/1000*view().max);drawCv()});
addEventListener('resize',()=>{drawEq();drawCv()});
head();tabs();trades();
if(D.moedas.length){const t=D.trades[0];if(t){const i=D.moedas.findIndex(m=>m.nome===t.m);cur=Math.max(0,i);start=Math.max(0,idxOf(D.moedas[cur].c,t.ts0)-90);tabs()}else{start=1e9}}
drawEq();drawCv();
</script></body></html>
"""

AVISOS_BASE = [
    "Dinheiro simulado. Nenhuma ordem é enviada e não há carteira envolvida.",
    "Cada entrada acontece na abertura do candle seguinte ao sinal, com derrapagem e taxa nos dois lados. Quando stop e take profit cabem no mesmo candle, vale o stop.",
    "Só entram no teste moedas que existem hoje. As que morreram não aparecem, e isso faz o histórico parecer melhor do que seria na época (viés de sobrevivência).",
    "Os sinais usam preço e volume. Baleias, influenciadores e bundles não têm histórico gratuito, então não entram no backtest. O RugCheck só vale para o estado atual do token.",
    "Poucas operações dizem pouco. Compare o resultado com comprar e segurar e olhe os dois lados do período antes de tirar conclusão.",
    "Passado não garante futuro, e memecoins podem perder liquidez de uma hora para outra, o que um backtest de candles não mostra.",
]


# ---------------------------------------------------------------- comandos de simulação
def rodar(moedas, cfg, caminho, modo, avisos, inicio_ts=None, fechar_fim=True):
    res = simular(moedas, cfg, inicio_ts=inicio_ts, fechar_fim=fechar_fim)
    st = estatisticas(res, cfg)
    resumo_terminal(st, cfg)
    gerar_relatorio(res, st, cfg, caminho, modo, avisos)
    return res, st


def cmd_backtest(a):
    cfg = cfg_dos_args(a)
    moedas = carregar_moedas("dados")
    if not moedas:
        sys.exit("Sem dados em dados/. Rode antes: python mesa_agentes.py baixar")
    avisos = list(AVISOS_BASE)
    if a.com_rug:
        cache = ler_json("dados/rug.json", {}) if Path("dados/rug.json").exists() else {}
        for m in moedas:
            m["rug_ok"] = checar_rug(m["mint"], cache)
            m["rug_checado"] = True
            print("RugCheck %-14s %s" % (m["nome"], "ok" if m["rug_ok"] else "BLOQUEADA"))
        gravar_json("dados/rug.json", cache)
        avisos.append("Rug Gate aplicado com o estado de hoje do token a todo o histórico (olha para o futuro).")
    res, st = rodar(moedas, cfg, "relatorio.html", "BACKTEST · DADOS REAIS · DINHEIRO SIMULADO", avisos)
    gravar_json("resultado.json", {"stats": st, "trades": res["trades"], "cfg": cfg})


def cmd_tudo(a):
    a.agg = a.agg or CFG["agg"]
    cmd_descobrir(a)
    cmd_baixar(a)
    cmd_backtest(a)


def cmd_ao_vivo(a):
    cfg = cfg_dos_args(a)
    pools = ler_json("pools.json", [])
    if not pools:
        sys.exit("pools.json vazio. Rode antes: python mesa_agentes.py descobrir")
    est = ler_json("estado.json", {})
    if "inicio" not in est:
        est["inicio"] = agora()
        gravar_json("estado.json", est)
    print("Paper trading iniciado em %s UTC. Ctrl+C para parar." % fmt_ts(est["inicio"]))
    cache = ler_json("dados/rug.json", {}) if Path("dados/rug.json").exists() else {}
    vistos = set(est.get("vistos", []))
    # guarda pelo menos os últimos 4000 candles e tudo desde o início do paper trading (mais aquecimento)
    cutoff = min(est["inicio"] - 300 * cfg["agg"] * 60, agora() - 4000 * cfg["agg"] * 60)
    while True:
        for pool in pools:
            try:
                salvar_pool(Path("dados"), pool, baixar_candles(pool["addr"], 1, cfg["agg"]), cutoff)
            except Exception as e:
                print("aviso: %s: %s" % (pool["nome"], e))
        enderecos = {p["addr"] for p in pools}
        moedas = [m for m in carregar_moedas("dados") if m["addr"] in enderecos]
        for m in moedas:
            m["rug_ok"] = checar_rug(m["mint"], cache)
            m["rug_checado"] = True
        Path("dados").mkdir(exist_ok=True)
        gravar_json("dados/rug.json", cache)
        fim_ts = ts_fim(cfg["fim_data"])
        encerrado = agora() > fim_ts
        for m in moedas:
            m["c"] = [r for r in m["c"] if r[0] <= fim_ts]
        separar_viva(moedas, cfg["agg"])
        pareceres = est.setdefault("rug_por_candle", {})
        for m in moedas:
            por_candle = pareceres.setdefault(m["addr"], {})
            m["rug_por_candle"] = por_candle
            if m["c"] and not encerrado:
                ultimo = m["c"][-1][0]
                if ultimo + 2 * cfg["agg"] * 60 > agora():
                    por_candle.setdefault(str(ultimo), m["rug_ok"])
        res = simular(moedas, cfg, inicio_ts=est["inicio"], fechar_fim=encerrado)
        st = estatisticas(res, cfg)
        if encerrado:
            res["pendentes"] = []
        for k in res["marks"]:
            if k["ts"] < est["inicio"]:
                continue
            chave = "%s|%d|%s|%s" % (k["m"], k["ts"], k["tipo"], k.get("motivo", ""))
            if chave not in vistos:
                vistos.add(chave)
                print("[%s] %-8s %-14s a %.10f" % (fmt_ts(k["ts"]), k["tipo"].upper(), k["m"], k["px"]))
                with open("paper_log.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(k, ensure_ascii=False) + "\n")
        est["vistos"] = sorted(vistos)
        gravar_json("estado.json", est)
        avisos = list(AVISOS_BASE) + [
            "Modo ao vivo: a simulação conta desde %s; o que vem antes só serve de histórico para os indicadores." % fmt_dia(est["inicio"]),
            "Cada posição simulada é de %.0f%% da banca ($%.2f). Numa operação real com valores pequenos assim, taxas fixas de rede e a derrapagem de pools pequenos pesam bem mais do que no modelo." % (cfg["tamanho"] * 100, cfg["capital"] * cfg["tamanho"]),
            "As decisões acontecem quando cada candle de %d minutos fecha. A página verifica dados a cada minuto. O GitHub agenda as coletas a cada cinco minutos e pode atrasar. O candle em formação só aparece no gráfico." % cfg["agg"],
        ]
        modo = "%s · %s A %s · BANCA SIMULADA $%.0f · DADOS REAIS" % (
            "ENCERRADO" if encerrado else "AO VIVO", fmt_dia(est["inicio"]), fmt_dia(fim_ts), cfg["capital"])
        gerar_relatorio(res, st, cfg, "relatorio_ao_vivo.html" if a.html else None,
                        modo, avisos, json_path="vivo.json", max_candles=700)
        if encerrado:
            gravar_json("resultado.json", {"encerrado_em": agora(), "fim_previsto": fim_ts, "estatisticas": st,
                                             "operacoes": res["trades"], "banca_inicial": cfg["capital"]})
            Path("ENCERRADO").write_text("acompanhamento encerrado em %s UTC\n" % fmt_ts(agora()))
            print("Acompanhamento encerrado. Resultado final: $%.2f (%+.2f%%)" % (st["eq_final"], st["retorno_pct"]))
            return
        if a.uma_vez:
            print("Patrimônio simulado: $%.2f | abertas: %d" % (st["eq_final"], len(res["abertas"])))
            return
        print("Patrimônio simulado: $%.2f | abertas: %d | próxima rodada em %d min" % (st["eq_final"], len(res["abertas"]), a.intervalo))
        time.sleep(a.intervalo * 60)


# ---------------------------------------------------------------- demo offline
def gerar_demo(seed=7, n_moedas=8, n=2500, agg=5):
    R = random.Random(seed)
    t0 = agora() - n * agg * 60
    moedas = []
    for k in range(n_moedas):
        preco = 0.0001 * (0.3 + R.random() * 3)
        vol = 0.012 + R.random() * 0.02
        vbase = 300 + R.random() * 2000
        c, fase, resta = [], 0, 0
        for i in range(n):
            ts = t0 + i * agg * 60
            if resta <= 0:
                if fase == 1:
                    fase, resta = 2, R.randint(10, 40)
                elif fase == 2 or R.random() > 0.004:
                    fase, resta = 0, 1
                else:
                    fase, resta = 1, R.randint(6, 20)
            resta -= 1
            drift = {0: -0.0003, 1: R.uniform(0.02, 0.07), 2: -R.uniform(0.01, 0.05)}[fase]
            o = preco
            caminho = [o]
            for _ in range(5):
                preco = max(1e-9, preco * math.exp(R.gauss(0, vol / 2) + drift / 5))
                caminho.append(preco)
            v = vbase * R.uniform(0.4, 1.6) * (R.uniform(4, 10) if fase == 1 else 1)
            c.append([ts, o, max(caminho), min(caminho), preco, v])
        moedas.append({"nome": "$DEMO%d" % (k + 1), "addr": "demo%d" % k, "mint": "demo%d" % k, "c": c, "rug_ok": True})
    return moedas


def cmd_demo(a):
    cfg = cfg_dos_args(a)
    moedas = gerar_demo()
    avisos = ["DEMO: candles inventados (com pumps e dumps sorteados). Serve só para testar o motor e ver como o relatório funciona."] + AVISOS_BASE
    res, st = rodar(moedas, cfg, "relatorio_demo.html", "DEMO · DADOS SINTÉTICOS · DINHEIRO SIMULADO", avisos)
    gravar_json("resultado_demo.json", {"stats": st, "trades": res["trades"], "cfg": cfg})


# ---------------------------------------------------------------- página ao vivo
def modelo_vivo():
    """Mesma página do relatório, mas buscando o vivo.json (branch 'vivo' do próprio repositório) a cada minuto."""
    t = MODELO.replace("const D=__DADOS__;", "let D=null;")
    ini = t.index("head();tabs();trades();")
    fim = t.index("</script></body></html>")
    novo = r"""let primeiro=true,carregando=false;
function aplicar(novo){
 if(!novo.moedas||!novo.moedas.length){$('#gerado').textContent='sem moedas ainda';return}
 const seguindo=primeiro||(D&&start>=view().max);
 const nome=D&&D.moedas[cur]?D.moedas[cur].nome:null;
 D=novo;
 const i=nome?D.moedas.findIndex(m=>m.nome===nome):-1;cur=i>=0?i:0;
 if(seguindo)start=1e9;
 head();
 $('#split').textContent='';
 const kb=$('#kpis').lastElementChild;if(kb&&kb.textContent.includes('Comprar e segurar'))kb.remove();
 tabs();trades();drawEq();drawCv();primeiro=false;
}
async function buscar(){
 if(carregando)return;carregando=true;
 try{
  const u='LicitaZero',r='mesa-agentes';
  const resp=await fetch('https://raw.githubusercontent.com/'+u+'/'+r+'/vivo/vivo.json?t='+Date.now(),{cache:'no-store'});
  if(!resp.ok)throw new Error('HTTP '+resp.status);
  aplicar(await resp.json());
  $('#gerado').textContent+=' · verificado '+new Date().toLocaleTimeString('pt-BR');
 }catch(e){
  $('#gerado').textContent=D?('sem resposta agora ('+e.message+'); mostrando os últimos dados'):('aguardando a primeira rodada ('+e.message+')');
 }
 carregando=false;
}
buscar();setInterval(buscar,60000);
"""
    return t[:ini] + novo + t[fim:]


def cmd_viewer(a):
    Path(a.saida).parent.mkdir(parents=True, exist_ok=True)
    with open(a.saida, "w", encoding="utf-8") as f:
        f.write(modelo_vivo())
    print("Página ao vivo salva em %s" % a.saida)


# ---------------------------------------------------------------- main
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Mesa de Agentes: paper trading de memecoins com dados reais.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def comuns(p):
        p.add_argument("--capital", type=float)
        p.add_argument("--tamanho", type=float, help="fração do patrimônio por entrada (0.02 = 2%%)")
        p.add_argument("--stop", type=float)
        p.add_argument("--tp1", type=float)
        p.add_argument("--tp2", type=float)
        p.add_argument("--slip", type=float)
        p.add_argument("--max-pos", dest="max_pos", type=int)
        p.add_argument("--min-sinais", dest="min_sinais", type=int)
        p.add_argument("--agg", type=int, choices=[1, 5, 15], help="minutos por candle")
        p.add_argument("--ate", help="último dia do acompanhamento ao vivo (AAAA-MM-DD)")

    def desc(p):
        p.add_argument("--top", type=int, default=5)
        p.add_argument("--paginas", type=int, default=3)
        p.add_argument("--min-liq", dest="min_liq", type=float, default=30000)
        p.add_argument("--min-vol24", type=float, default=20000)
        p.add_argument("--min-idade-h", dest="min_idade_h", type=float, default=72)

    p = sub.add_parser("demo"); comuns(p); p.set_defaults(f=cmd_demo)
    p = sub.add_parser("descobrir"); desc(p); p.set_defaults(f=cmd_descobrir)
    p = sub.add_parser("baixar"); p.add_argument("--paginas", type=int, default=3); p.add_argument("--agg", type=int, default=5, choices=[1, 5, 15]); p.set_defaults(f=cmd_baixar)
    p = sub.add_parser("backtest"); comuns(p); p.add_argument("--com-rug", action="store_true"); p.set_defaults(f=cmd_backtest)
    p = sub.add_parser("tudo"); comuns(p); desc(p); p.add_argument("--com-rug", action="store_true"); p.set_defaults(f=cmd_tudo)
    p = sub.add_parser("ao-vivo"); comuns(p); p.add_argument("--intervalo", type=int, default=5, help="minutos entre rodadas"); p.add_argument("--uma-vez", dest="uma_vez", action="store_true", help="faz uma rodada e sai (para agendadores na nuvem)"); p.add_argument("--html", action="store_true", help="também grava relatorio_ao_vivo.html completo"); p.set_defaults(f=cmd_ao_vivo)

    p = sub.add_parser("viewer"); p.add_argument("--saida", default="site/index.html"); p.set_defaults(f=cmd_viewer)

    a = ap.parse_args()
    if a.cmd == "tudo":
        a.paginas = getattr(a, "paginas", 3)
    a.f(a)


if __name__ == "__main__":
    main()
