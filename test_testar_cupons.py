#!/usr/bin/env python3
"""Testes automatizados do testar_cupons.py.

Sobe o mock_api.py numa porta livre e roda o testador contra ele como subprocesso,
conferindo os códigos de saída, o resumo e os arquivos CSV/JSON. Não toca na API
real e não precisa de rede.

    python3 -m unittest test_testar_cupons -v
    # ou, se tiver pytest:
    pytest test_testar_cupons.py -v

Requer apenas a biblioteca padrão + curl (como o próprio projeto).
"""
import csv
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

BASE = Path(__file__).resolve().parent
EVENT_ID = "11111111-2222-3333-4444-555555555555"


def porta_livre():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServer:
    """Sobe o mock_api.py numa porta dada e espera ele responder."""

    def __init__(self, porta):
        self.porta = porta
        self.proc = None

    def __enter__(self):
        # roda uma cópia do mock com a porta trocada, para não colidir com a 8765
        fonte = (BASE / "mock_api.py").read_text(encoding="utf-8")
        fonte = fonte.replace('("127.0.0.1", 8765)', f'("127.0.0.1", {self.porta})')
        self._tmp = tempfile.NamedTemporaryFile(
            "w", suffix="_mock.py", delete=False, encoding="utf-8")
        self._tmp.write(fonte)
        self._tmp.close()
        self.proc = subprocess.Popen([sys.executable, self._tmp.name])
        self._esperar()
        return self

    def _esperar(self, tentativas=50):
        url = f"http://127.0.0.1:{self.porta}/v1/events/{EVENT_ID}"
        for _ in range(tentativas):
            try:
                urlopen(url, timeout=1).read()
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("mock_api não respondeu a tempo")

    def __exit__(self, *a):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        Path(self._tmp.name).unlink(missing_ok=True)


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.porta = porta_livre()
        cls.server = MockServer(cls.porta).__enter__()
        cls.api_base = f"http://127.0.0.1:{cls.porta}"

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__()

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="test_cupons_"))

    def rodar(self, codigos, *extra):
        """Escreve uma lista, roda o testador contra o mock e devolve o resultado."""
        lista = self.dir / "lista.txt"
        lista.write_text("\n".join(codigos) + "\n", encoding="utf-8")
        out_csv = self.dir / "r.csv"
        out_json = self.dir / "r.json"
        cmd = [
            sys.executable, str(BASE / "testar_cupons.py"), str(lista),
            "--url", f"https://cart.ingresse.com/{EVENT_ID}/tickets",
            "--api-base", self.api_base,
            "--cache", str(self.dir / "cache.jsonl"),
            "--out-csv", str(out_csv), "--out-json", str(out_json),
            "--workers", "5", "--pause", "0",
            *extra,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.csv_path, self.json_path = out_csv, out_json
        return proc

    def ler_json(self):
        return json.loads(self.json_path.read_text(encoding="utf-8"))

    def ler_csv(self):
        with self.csv_path.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))


class TestValidacao(BaseCase):
    def test_detecta_validos_e_invalidos(self):
        proc = self.rodar(["ROMAEVENTOS", "HYPE24HSINTRO", "NAOEXISTE", "OUTRO"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dados = {d["code"]: d for d in self.ler_json()}
        self.assertTrue(dados["ROMAEVENTOS"]["valid"])
        self.assertEqual(dados["ROMAEVENTOS"]["discount"], 50)
        self.assertTrue(dados["HYPE24HSINTRO"]["valid"])
        self.assertFalse(dados["NAOEXISTE"]["valid"])
        self.assertFalse(dados["OUTRO"]["valid"])

    def test_csv_bate_com_json(self):
        self.rodar(["ROMAEVENTOS", "NAOEXISTE"])
        linhas = {l["codigo"]: l for l in self.ler_csv()}
        self.assertEqual(linhas["ROMAEVENTOS"]["valido"], "SIM")
        self.assertEqual(linhas["ROMAEVENTOS"]["desconto_pct"], "50")
        self.assertEqual(linhas["NAOEXISTE"]["valido"], "nao")

    def test_normaliza_e_remove_duplicados(self):
        # minúsculas, acento e repetição devem virar um único ROMAEVENTOS
        self.rodar(["romaeventos", "ROMAEVENTOS", "Rómaeventos"])
        dados = self.ler_json()
        self.assertEqual(len(dados), 1)
        self.assertEqual(dados[0]["code"], "ROMAEVENTOS")

    def test_ignora_comentarios(self):
        proc = self.rodar(["# cabeçalho", "ROMAEVENTOS  # valido", "", "NAOEXISTE"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        codes = {d["code"] for d in self.ler_json()}
        self.assertEqual(codes, {"ROMAEVENTOS", "NAOEXISTE"})


class TestSaidas(BaseCase):
    def test_rate_limit_sai_3(self):
        # códigos começando com RL recebem 429 no mock
        proc = self.rodar(["RL001", "RL002"])
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)

    def test_erro_de_rede_sai_2(self):
        # códigos começando com ERR recebem 500 no mock -> falha de rede (sem bloqueio)
        proc = self.rodar(["ERR001"], "--retries", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_info_mostra_evento_e_sai_0(self):
        proc = self.rodar(["ROMAEVENTOS"], "--info")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("aceita cupom: sim", proc.stdout)
        self.assertFalse(self.json_path.exists(), "não deveria testar nada com --info")


class TestCache(BaseCase):
    def test_retoma_do_cache(self):
        cache = self.dir / "cache.jsonl"
        lista = self.dir / "lista.txt"
        lista.write_text("ROMAEVENTOS\nNAOEXISTE\n", encoding="utf-8")
        base_cmd = [
            sys.executable, str(BASE / "testar_cupons.py"), str(lista),
            "--url", f"https://cart.ingresse.com/{EVENT_ID}/tickets",
            "--api-base", self.api_base, "--cache", str(cache),
            "--out-csv", str(self.dir / "r.csv"),
            "--out-json", str(self.dir / "r.json"),
            "--workers", "5", "--pause", "0",
        ]
        subprocess.run(base_cmd, capture_output=True, text=True, check=True)
        self.assertEqual(len(cache.read_text().splitlines()), 2)

        # segunda rodada: tudo já está no cache, nada novo a testar
        proc = subprocess.run(base_cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("a testar: 0", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
