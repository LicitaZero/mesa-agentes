# -*- coding: utf-8 -*-
"""Testes offline da seleção de moedas (descobrir e baixar --manter). Nenhuma chamada de rede."""
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mesa_agentes as ma  # noqa: E402

T0 = 1_700_000_000


def candles(n, amp, vol, preco=1.0):
    """n candles de 5 min com amplitude relativa 'amp' e volume 'vol'."""
    return {T0 + i * 300: [T0 + i * 300, preco, preco * (1 + amp), preco, preco, vol] for i in range(n)}


def pool(addr, nome, fdv=1e7, mint=None, liq=80000.0):
    return {"attributes": {"address": addr, "name": "%s / SOL" % nome, "reserve_in_usd": str(liq),
                           "volume_usd": {"h24": "1000000"}, "fdv_usd": str(fdv),
                           "pool_created_at": "2025-01-01T00:00:00Z"},
            "relationships": {"base_token": {"data": {"id": "solana_%s" % (mint or ("MINT" + addr))}}}}


class EmPastaTemporaria(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        for alvo, valor in (("PAUSA_API", 0),):
            p = mock.patch.object(ma, alvo, valor)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()


class TestEscolherPools(unittest.TestCase):
    def test_ordem_por_volume_e_limite(self):
        mets = {a: {"amp": 0.02, "vol": v, "n": 500} for a, v in (("a", 100), ("b", 900), ("c", 500), ("d", 300))}
        escolhidos, fora = ma.escolher_pools(mets, 2)
        self.assertEqual(escolhidos, ["b", "c"])
        self.assertEqual(sorted(a for a, _ in fora), ["a", "d"])

    def test_descarta_pouco_volatil_pouco_volume_e_poucos_candles(self):
        mets = {
            "stable": {"amp": 0.0004, "vol": 9e6, "n": 500},
            "morta": {"amp": 0.03, "vol": 5.0, "n": 500},
            "curta": None,
            "meme": {"amp": 0.03, "vol": 400.0, "n": 500},
        }
        escolhidos, fora = ma.escolher_pools(mets, 5)
        self.assertEqual(escolhidos, ["meme"])
        self.assertEqual(dict(fora)["stable"], "pouco volátil (não parece memecoin)")
        self.assertEqual(dict(fora)["morta"], "pouco volume")
        self.assertEqual(dict(fora)["curta"], "poucos candles")

    def test_metricas(self):
        lst = list(candles(400, 0.02, 250.0).values())
        m = ma.metricas_pool(lst)
        self.assertAlmostEqual(m["amp"], 0.02)
        self.assertAlmostEqual(m["vol"], 250.0)
        self.assertIsNone(ma.metricas_pool(lst[:100]))


class TestDescobrir(EmPastaTemporaria):
    def test_filtros_de_nome_mint_e_valor_de_mercado(self):
        itens = [
            pool("p1", "BONKISH"),
            pool("p2", "SOL"),
            pool("p3", "COISA", mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
            pool("p4", "HYPE", fdv=3e10),
            pool("p5", "WETH"),
            pool("p6", "PEPEZ", liq=1000.0),
            pool("p7", "GATO"),
        ]
        chamadas = []

        def falso(url, tentativas=4):
            chamadas.append(url)
            return {"data": itens}

        a = Namespace(top=20, paginas=1, min_liq=30000, min_idade_h=72, max_fdv=2e9)
        with mock.patch.object(ma, "http_get", falso):
            ma.cmd_descobrir(a)
        nomes = [p["nome"] for p in ma.ler_json("pools.json", [])]
        self.assertEqual(sorted(nomes), ["BONKISH", "GATO"])
        self.assertTrue(all("trending_pools" in u for u in chamadas), chamadas)


class TestBaixarManter(EmPastaTemporaria):
    def test_fica_so_com_as_mais_ativas_e_volateis(self):
        base = [{"addr": a, "nome": n, "mint": "M" + a, "liq": 1.0, "vol24": 1.0, "fdv": 1.0, "idade_h": 99.0}
                for a, n in (("a", "MEME1"), ("b", "MEME2"), ("c", "STABLE"), ("d", "MEME3"))]
        ma.gravar_json("pools.json", base)
        series = {"a": candles(400, 0.03, 900.0), "b": candles(400, 0.02, 500.0),
                  "c": candles(400, 0.0003, 9e6), "d": candles(400, 0.04, 700.0)}
        with mock.patch.object(ma, "baixar_candles", lambda addr, paginas, agg: series[addr]):
            ma.cmd_baixar(Namespace(paginas=1, agg=5, manter=2))
        mantidas = [p["nome"] for p in ma.ler_json("pools.json", [])]
        self.assertEqual(mantidas, ["MEME1", "MEME3"])
        self.assertEqual(sorted(x.name for x in Path("dados").glob("*.json")), ["a.json", "d.json"])

    def test_sem_manter_nada_muda(self):
        base = [{"addr": "a", "nome": "X", "mint": "Ma", "liq": 1.0, "vol24": 1.0, "fdv": 1.0, "idade_h": 99.0}]
        ma.gravar_json("pools.json", base)
        with mock.patch.object(ma, "baixar_candles", lambda addr, paginas, agg: candles(400, 0.0001, 1.0)):
            ma.cmd_baixar(Namespace(paginas=1, agg=5))
        self.assertEqual(len(ma.ler_json("pools.json", [])), 1)


if __name__ == "__main__":
    unittest.main()
