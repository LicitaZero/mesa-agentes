# -*- coding: utf-8 -*-
"""Testes determinísticos e offline do simulador (sinais e simular).

Usam candles sintéticos fixos no formato [ts, abertura, máxima, mínima, fechamento, volume].
Toda chamada de rede é bloqueada durante os testes.
"""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mesa_agentes as ma  # noqa: E402

T0 = 1_700_000_000
PASSO = 5 * 60  # candles de 5 minutos

# Fatores de custo da configuração padrão (slip 1,5% e taxa 0,25% em cada lado).
CUSTO_COMPRA = 1.015 * 1.0025
LIQ_VENDA = 0.985 * 0.9975

_patches = []


def _sem_rede(*args, **kwargs):
    raise AssertionError("chamada de rede durante os testes")


def setUpModule():
    for alvo in ("urllib.request.urlopen", "socket.create_connection", "socket.socket.connect"):
        p = mock.patch(alvo, side_effect=_sem_rede)
        p.start()
        _patches.append(p)


def tearDownModule():
    while _patches:
        _patches.pop().stop()


def cfg_teste(**extra):
    """Configuração padrão com janelas curtas para caber em séries pequenas."""
    cfg = dict(ma.CFG)
    cfg.update({"min_hist": 5, "jan_vol": 5, "jan_brk": 5})
    cfg.update(extra)
    return cfg


def candle(i, o, h, l, c, v=100.0, ts=None):
    return [T0 + i * PASSO if ts is None else ts, o, h, l, c, v]


def serie(depois, nome="$TESTE"):
    """6 candles planos em 1.0, um candle de sinal (volume + rompimento) no índice 6
    e depois os candles dados como (abertura, máxima, mínima, fechamento)."""
    c = [candle(i, 1.0, 1.0, 1.0, 1.0) for i in range(6)]
    c.append(candle(6, 1.0, 1.1, 1.0, 1.1, v=1000.0))
    for j, (o, h, l, cl) in enumerate(depois):
        c.append(candle(7 + j, o, h, l, cl))
    return {"nome": nome, "c": c, "rug_ok": True}


PLANO = (1.0, 1.0, 1.0, 1.0)


class TestSinais(unittest.TestCase):
    def test_sinais_exatos(self):
        cfg = cfg_teste()
        m = serie([PLANO] * 3)
        s = ma.sinais(m["c"], cfg)
        self.assertEqual(len(s), 10)
        # antes de min_hist nada é avaliado
        for i in range(5):
            self.assertEqual(s[i], (0, []))
        self.assertEqual(s[5], (0, []))
        self.assertEqual(s[6], (2, ["volume", "rompimento"]))
        # depois do pico, sem volume e sem novo rompimento
        self.assertEqual(s[7], (0, []))

    def test_pressao_com_tres_verdes(self):
        cfg = cfg_teste()
        c = [candle(i, 1.0, 1.0, 1.0, 1.0) for i in range(5)]
        # três velas verdes seguidas, fechando perto da máxima e sem romper a máxima anterior
        c.append(candle(5, 0.90, 0.95, 0.90, 0.94))
        c.append(candle(6, 0.94, 0.97, 0.94, 0.96))
        c.append(candle(7, 0.96, 0.99, 0.96, 0.98))
        s = ma.sinais(c, cfg)
        self.assertEqual(s[7], (1, ["pressão"]))

    def test_volume_ignorado_em_pool_morta(self):
        cfg = cfg_teste()
        c = [candle(i, 1.0, 1.0, 1.0, 1.0, v=10.0) for i in range(6)]
        c.append(candle(6, 1.0, 1.1, 1.0, 1.1, v=1000.0))
        s = ma.sinais(c, cfg)
        # mediana 10 < vol_med_min 100: só o rompimento conta
        self.assertEqual(s[6], (1, ["rompimento"]))


class TestSimular(unittest.TestCase):
    def test_entrada_na_abertura_do_candle_seguinte(self):
        cfg = cfg_teste()
        m = serie([(1.05, 1.05, 1.05, 1.05)] * 4)
        res = ma.simular([m], cfg)
        self.assertEqual(len(res["trades"]), 1)
        t = res["trades"][0]
        compra = [x for x in res["marks"] if x["tipo"] == "compra"]
        self.assertEqual(compra, [{"m": "$TESTE", "ts": T0 + 7 * PASSO, "tipo": "compra", "px": 1.05, "custo": 10.0}])
        self.assertEqual(t["ts0"], T0 + 7 * PASSO)  # candle seguinte ao sinal (índice 6)
        self.assertEqual(t["px0"], 1.05)            # abertura do candle 7, não o fechamento 1.1 do sinal
        self.assertEqual(t["sinais"], ["volume", "rompimento"])
        self.assertEqual(t["motivos"], ["fim"])
        self.assertAlmostEqual(t["custo"], 10.0, places=12)  # 20% de $50
        self.assertAlmostEqual(t["pnl"], -0.343967667039, places=9)  # só custos de ida e volta
        self.assertAlmostEqual(res["curva"][-1][1], 49.656, places=4)

    def test_rug_atual_nao_reescreve_parecer_anterior(self):
        moeda = serie([PLANO] * 4)
        moeda["rug_por_candle"] = {str(T0 + 6 * PASSO): True}
        moeda["rug_ok"] = False
        resultado = ma.simular([moeda], cfg_teste())
        self.assertEqual(resultado["trades"][0]["ts0"], T0 + 7 * PASSO)

    def test_parecer_ausente_bloqueia_sinal_ao_vivo(self):
        moeda = serie([PLANO] * 4)
        moeda["rug_por_candle"] = {}
        resultado = ma.simular([moeda], cfg_teste())
        self.assertEqual(resultado["trades"], [])

    def test_candle_em_formacao_fica_fora_do_motor(self):
        moeda = serie([PLANO] * 3)
        ultimo = moeda["c"][-1]
        with mock.patch.object(ma, "agora", return_value=ultimo[0] + 30):
            ma.separar_viva([moeda], 5)
        self.assertEqual(moeda["c_viva"], ultimo)
        self.assertNotIn(ultimo, moeda["c"])

    def test_sinal_sem_candle_seguinte_fica_pendente(self):
        cfg = cfg_teste()
        m = serie([])
        # um candle plano extra no início: o simulador exige mais de min_hist + 2 candles
        m["c"] = [candle(-1, 1.0, 1.0, 1.0, 1.0)] + m["c"]
        res = ma.simular([m], cfg)
        self.assertEqual(res["trades"], [])
        self.assertEqual(res["pendentes"], [{"m": "$TESTE", "ts": T0 + 6 * PASSO,
                                             "sinais": ["volume", "rompimento"]}])
        self.assertEqual(res["curva"][-1][1], 50.0)

    def test_pendente_expira_com_buraco_nos_dados(self):
        cfg = cfg_teste()
        m = serie([])
        # próximo candle chega 20 min depois do sinal (> 3 candles de 5 min): sem entrada
        m["c"].append(candle(0, 1.0, 1.0, 1.0, 1.0, ts=T0 + 6 * PASSO + 20 * 60))
        res = ma.simular([m], cfg)
        self.assertEqual(res["trades"], [])
        self.assertFalse(any(x["tipo"] == "compra" for x in res["marks"]))

    def test_stop_tem_precedencia_sobre_alvo_no_mesmo_candle(self):
        cfg = cfg_teste()
        # candle 8 toca a mínima 0.85 (stop em 0.88) e a máxima 1.35 (TP1 em 1.30)
        m = serie([PLANO, (1.0, 1.35, 0.85, 1.0), PLANO])
        res = ma.simular([m], cfg)
        self.assertEqual(len(res["trades"]), 1)
        t = res["trades"][0]
        self.assertEqual(t["motivos"], ["stop"])
        self.assertEqual(t["px1"], 0.88)
        self.assertEqual(t["ts1"], T0 + 8 * PASSO)
        self.assertAlmostEqual(t["pnl"], -1.502691546995, places=9)
        self.assertEqual([x["tipo"] for x in res["marks"]], ["compra", "stop"])

    def test_stop_com_gap_vende_na_abertura(self):
        cfg = cfg_teste()
        m = serie([PLANO, (0.80, 0.82, 0.75, 0.80), PLANO])
        t = ma.simular([m], cfg)["trades"][0]
        self.assertEqual(t["motivos"], ["stop"])
        self.assertEqual(t["px1"], 0.80)
        self.assertAlmostEqual(t["pnl"], -2.275174133631, places=9)

    def test_tp1_e_tp2_no_mesmo_candle(self):
        cfg = cfg_teste()
        m = serie([PLANO, (1.0, 1.7, 1.0, 1.65), PLANO])
        res = ma.simular([m], cfg)
        t = res["trades"][0]
        self.assertEqual(t["motivos"], ["TP1", "TP2"])
        self.assertEqual(t["px1"], 1.6)
        self.assertAlmostEqual(t["pnl"], 4.001246882793, places=9)
        self.assertAlmostEqual(res["curva"][-1][1], 54.0012, places=4)

    def test_tp1_move_stop_para_entrada(self):
        cfg = cfg_teste()
        m = serie([PLANO, (1.0, 1.35, 1.0, 1.2), (1.2, 1.2, 0.95, 1.0), PLANO])
        t = ma.simular([m], cfg)["trades"][0]
        self.assertEqual(t["motivos"], ["TP1", "stop (empate)"])
        self.assertEqual(t["px1"], 1.0)
        self.assertAlmostEqual(t["pnl"], 1.104437182905, places=9)


class TestConsistenciaDoCaixa(unittest.TestCase):
    def _checar(self, res, cfg):
        soma_pnl = sum(t["pnl"] for t in res["trades"])
        self.assertEqual(res["abertas"], [])
        self.assertAlmostEqual(res["curva"][-1][1], cfg["capital"] + soma_pnl, places=3)
        for t in res["trades"]:
            self.assertAlmostEqual(t["pnl"], t["custo"] * t["pct"] / 100, places=9)
        self.assertTrue(all(e > 0 for _, e in res["curva"]))

    def test_cenarios_fixos(self):
        cfg = cfg_teste()
        moedas = [
            serie([PLANO, (1.0, 1.7, 1.0, 1.65), PLANO], nome="$A"),
            serie([PLANO, (1.0, 1.35, 0.85, 1.0), PLANO], nome="$B"),
            serie([(1.05, 1.05, 1.05, 1.05)] * 3, nome="$C"),
        ]
        res = ma.simular(moedas, cfg)
        self.assertEqual(len(res["trades"]), 3)
        # as três entram no mesmo candle. A 1ª usa 20% de $50; as seguintes, 20% do patrimônio
        # marcado a mercado, em que cada posição aberta vale custo / (1.015 * 1.0025)
        custos = [t["custo"] for t in sorted(res["trades"], key=lambda t: t["m"])]
        a = 10.0
        b = 0.2 * (40.0 + a / CUSTO_COMPRA)
        c = 0.2 * (40.0 - b + (a + b) / CUSTO_COMPRA)
        self.assertAlmostEqual(custos[0], a, places=12)
        self.assertAlmostEqual(custos[1], b, places=12)
        self.assertAlmostEqual(custos[2], c, places=12)
        self.assertAlmostEqual(custos[1], 9.965530, places=6)
        self.assertAlmostEqual(custos[2], 9.931178, places=6)
        self._checar(res, cfg)

    def test_dados_demo_deterministicos(self):
        cfg = dict(ma.CFG)
        with mock.patch.object(ma, "agora", return_value=T0):
            r1 = ma.simular(ma.gerar_demo(seed=7, n_moedas=4, n=800), cfg)
            r2 = ma.simular(ma.gerar_demo(seed=7, n_moedas=4, n=800), cfg)
        self.assertGreater(len(r1["trades"]), 0)
        self.assertEqual([(t["m"], t["ts0"], t["ts1"]) for t in r1["trades"]],
                         [(t["m"], t["ts0"], t["ts1"]) for t in r2["trades"]])
        self.assertEqual(r1["curva"], r2["curva"])
        self._checar(r1, cfg)


class TestSemRede(unittest.TestCase):
    def test_rede_bloqueada(self):
        with self.assertRaises(AssertionError):
            socket.create_connection(("example.com", 443))


if __name__ == "__main__":
    unittest.main()
