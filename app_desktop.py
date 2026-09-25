"""Interface desktop da Automação XML Bling."""

import logging
import os
import queue
import sys
import threading
from datetime import date, datetime, time as horario, timedelta
from pathlib import Path


NOME_APP = "Automacao XML Bling"
BASE_DIR = Path(__file__).resolve().parent


def pasta_do_aplicativo():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return BASE_DIR


def preparar_ambiente():
    pasta_app = pasta_do_aplicativo()
    if getattr(sys, "frozen", False):
        local_app_data = Path(
            os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        pasta_usuario = local_app_data / NOME_APP
        pasta_documentos = Path(os.getenv("USERPROFILE", Path.home())) / "Documents"
        pasta_dados = pasta_documentos / NOME_APP
    else:
        pasta_usuario = BASE_DIR
        pasta_dados = BASE_DIR

    pasta_usuario.mkdir(parents=True, exist_ok=True)
    pasta_dados.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("AUTOMACAO_DATA_DIR", str(pasta_dados))
    os.environ.setdefault("AUTOMACAO_ENV_FILE", str(pasta_app / ".env"))
    os.environ.setdefault(
        "AUTOMACAO_CONFIG_FILE", str(pasta_app / "configuracao.json")
    )
    os.environ.setdefault("BLING_TOKENS_FILE", str(pasta_usuario / "tokens.json"))


preparar_ambiente()

import tkinter as tk
from tkinter import messagebox, ttk

from baixar_xmls import DATA_DIR, processar_download
from bling_auth import ENV_FILE, TOKENS_FILE
from organizar_xmls import CONFIG_FILE


COR_VERDE = "#009C4A"
COR_LARANJA = "#FF7F18"
COR_FUNDO = "#F5F7F6"
COR_TEXTO = "#24302A"


def ultima_janela_disponivel(agora=None):
    agora = agora or datetime.now()
    if agora.time() >= horario(14):
        return agora.date()
    return agora.date() - timedelta(days=1)


class HandlerFila(logging.Handler):
    def __init__(self, fila):
        super().__init__()
        self.fila = fila

    def emit(self, registro):
        try:
            self.fila.put(("log", self.format(registro)))
        except Exception:
            self.handleError(registro)


class Aplicacao(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Automação XML Bling")
        self.geometry("760x620")
        self.minsize(680, 560)
        self.configure(bg=COR_FUNDO)
        self.fila = queue.Queue()
        self.em_execucao = False
        self.ultimo_resultado = None

        self._configurar_estilo()
        self._montar_tela()
        self._atualizar_estado_conexao()
        self.after(100, self._processar_fila)
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

    def _configurar_estilo(self):
        estilo = ttk.Style(self)
        estilo.theme_use("clam")
        estilo.configure("TFrame", background=COR_FUNDO)
        estilo.configure("Card.TFrame", background="white")
        estilo.configure(
            "Title.TLabel",
            background=COR_FUNDO,
            foreground=COR_TEXTO,
            font=("Segoe UI", 20, "bold"),
        )
        estilo.configure(
            "Subtitle.TLabel",
            background=COR_FUNDO,
            foreground="#5D6B64",
            font=("Segoe UI", 10),
        )
        estilo.configure(
            "CardTitle.TLabel",
            background="white",
            foreground=COR_TEXTO,
            font=("Segoe UI", 11, "bold"),
        )
        estilo.configure(
            "Card.TLabel", background="white", foreground=COR_TEXTO
        )
        estilo.configure(
            "Accent.TButton",
            background=COR_VERDE,
            foreground="white",
            font=("Segoe UI", 10, "bold"),
            padding=(18, 10),
        )
        estilo.map(
            "Accent.TButton",
            background=[("active", "#007F3D"), ("disabled", "#A9BDB1")],
        )
        estilo.configure("Secondary.TButton", padding=(12, 8))
        estilo.configure("TRadiobutton", background="white", foreground=COR_TEXTO)

    def _montar_tela(self):
        cabecalho = ttk.Frame(self, padding=(28, 22, 28, 12))
        cabecalho.pack(fill="x")
        ttk.Label(
            cabecalho, text="Automação XML Bling", style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            cabecalho,
            text="Baixe e organize as NF-e de uma janela encerrada às 14h.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        conteudo = ttk.Frame(self, padding=(28, 0, 28, 22))
        conteudo.pack(fill="both", expand=True)

        cartao = ttk.Frame(conteudo, style="Card.TFrame", padding=20)
        cartao.pack(fill="x")
        cartao.columnconfigure(1, weight=1)

        ttk.Label(cartao, text="Conexão com o Bling", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.label_conexao = ttk.Label(cartao, style="Card.TLabel")
        self.label_conexao.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.botao_conectar = ttk.Button(
            cartao,
            text="Conectar ao Bling",
            style="Secondary.TButton",
            command=self._conectar,
        )
        self.botao_conectar.grid(row=1, column=1, sticky="e", pady=(8, 0))

        opcoes = ttk.Frame(conteudo, style="Card.TFrame", padding=20)
        opcoes.pack(fill="x", pady=(14, 0))
        opcoes.columnconfigure(1, weight=1)
        ttk.Label(opcoes, text="Processamento", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )

        ttk.Label(opcoes, text="Data da janela", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(14, 0), padx=(0, 18)
        )
        self.data_var = tk.StringVar(
            value=ultima_janela_disponivel().strftime("%d/%m/%Y")
        )
        self.campo_data = ttk.Entry(
            opcoes, textvariable=self.data_var, width=16, font=("Segoe UI", 11)
        )
        self.campo_data.grid(row=1, column=1, sticky="w", pady=(14, 0))

        ttk.Label(opcoes, text="Resultado", style="Card.TLabel").grid(
            row=2, column=0, sticky="nw", pady=(16, 0), padx=(0, 18)
        )
        self.modo_var = tk.StringVar(value="com_gnre")
        radios = ttk.Frame(opcoes, style="Card.TFrame")
        radios.grid(row=2, column=1, sticky="w", pady=(12, 0))
        ttk.Radiobutton(
            radios,
            text="NF-e organizadas + pasta GNRE",
            variable=self.modo_var,
            value="com_gnre",
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            radios,
            text="Somente NF-e organizadas",
            variable=self.modo_var,
            value="sem_gnre",
        ).pack(anchor="w", pady=2)
        ttk.Radiobutton(
            radios,
            text="Somente GNRE em ZIP (um arquivo por unidade)",
            variable=self.modo_var,
            value="somente_gnre_zip",
        ).pack(anchor="w", pady=2)

        acoes = ttk.Frame(opcoes, style="Card.TFrame")
        acoes.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        self.botao_executar = ttk.Button(
            acoes,
            text="Baixar e organizar",
            style="Accent.TButton",
            command=self._executar,
        )
        self.botao_executar.pack(side="left")
        self.botao_abrir = ttk.Button(
            acoes,
            text="Abrir pasta de resultado",
            style="Secondary.TButton",
            command=self._abrir_pasta,
        )
        self.botao_abrir.pack(side="left", padx=(10, 0))
        self.progresso = ttk.Progressbar(acoes, mode="indeterminate")
        self.progresso.pack(side="right", fill="x", expand=True, padx=(18, 0))

        painel_log = ttk.Frame(conteudo, style="Card.TFrame", padding=16)
        painel_log.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(painel_log, text="Acompanhamento", style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        self.log_texto = tk.Text(
            painel_log,
            height=10,
            wrap="word",
            state="disabled",
            bg="#F8FAF9",
            fg=COR_TEXTO,
            relief="flat",
            padx=10,
            pady=10,
            font=("Consolas", 9),
        )
        self.log_texto.pack(fill="both", expand=True)

    def _registrar(self, mensagem):
        self.log_texto.configure(state="normal")
        self.log_texto.insert("end", mensagem.rstrip() + "\n")
        self.log_texto.see("end")
        self.log_texto.configure(state="disabled")

    def _atualizar_estado_conexao(self):
        if TOKENS_FILE.exists():
            self.label_conexao.configure(text="Conectado neste computador")
            self.botao_conectar.configure(text="Reconectar")
        else:
            self.label_conexao.configure(text="Este computador ainda não está conectado")
            self.botao_conectar.configure(text="Conectar ao Bling")

    def _validar_arquivos(self, exigir_token=True):
        if not ENV_FILE.is_file():
            raise RuntimeError(f"Arquivo de credenciais não encontrado: {ENV_FILE}")
        if not CONFIG_FILE.is_file():
            raise RuntimeError(f"Arquivo de configuração não encontrado: {CONFIG_FILE}")
        if exigir_token and not TOKENS_FILE.is_file():
            raise RuntimeError("Conecte este computador ao Bling antes de executar.")

    def _definir_execucao(self, ativa):
        self.em_execucao = ativa
        estado = "disabled" if ativa else "normal"
        self.botao_executar.configure(state=estado)
        self.botao_conectar.configure(state=estado)
        self.campo_data.configure(state=estado)
        if ativa:
            self.progresso.start(12)
        else:
            self.progresso.stop()

    def _conectar(self):
        try:
            self._validar_arquivos(exigir_token=False)
        except RuntimeError as erro:
            messagebox.showerror("Configuração necessária", str(erro), parent=self)
            return
        self._definir_execucao(True)
        self._registrar("Abrindo a autorização do Bling no navegador...")
        threading.Thread(target=self._trabalho_autenticacao, daemon=True).start()

    def _trabalho_autenticacao(self):
        try:
            from autenticar_bling import main as autenticar

            autenticar()
            self.fila.put(("autenticacao_ok", None))
        except Exception as erro:
            self.fila.put(("erro", f"Não foi possível conectar ao Bling: {erro}"))

    def _ler_data(self):
        try:
            selecionada = datetime.strptime(self.data_var.get().strip(), "%d/%m/%Y").date()
        except ValueError as erro:
            raise RuntimeError("Informe a data no formato DD/MM/AAAA.") from erro
        limite = ultima_janela_disponivel()
        if selecionada > limite:
            raise RuntimeError(
                "Essa janela ainda não terminou. Selecione uma data até "
                f"{limite.strftime('%d/%m/%Y')}."
            )
        return selecionada

    def _executar(self):
        try:
            self._validar_arquivos()
            data_selecionada = self._ler_data()
        except RuntimeError as erro:
            messagebox.showerror("Não foi possível iniciar", str(erro), parent=self)
            return

        self._definir_execucao(True)
        self._registrar(
            f"Iniciando a janela {data_selecionada.strftime('%d/%m/%Y')}..."
        )
        modo = self.modo_var.get()
        incluir_gnre = modo == "com_gnre"
        somente_gnre_zip = modo == "somente_gnre_zip"
        threading.Thread(
            target=self._trabalho_download,
            args=(data_selecionada, incluir_gnre, somente_gnre_zip),
            daemon=True,
        ).start()

    def _trabalho_download(
        self,
        data_selecionada,
        incluir_gnre,
        somente_gnre_zip,
    ):
        try:
            handler = HandlerFila(self.fila)
            resultado = processar_download(
                data_selecionada,
                janela_14h=True,
                incluir_gnre=incluir_gnre,
                somente_gnre_zip=somente_gnre_zip,
                handler_log=handler,
            )
            self.fila.put(("resultado", resultado))
        except Exception as erro:
            self.fila.put(("erro", f"Processamento interrompido: {erro}"))

    def _processar_fila(self):
        try:
            while True:
                tipo, valor = self.fila.get_nowait()
                if tipo == "log":
                    self._registrar(valor)
                elif tipo == "autenticacao_ok":
                    self._definir_execucao(False)
                    self._atualizar_estado_conexao()
                    self._registrar("Conexão com o Bling concluída com sucesso.")
                    messagebox.showinfo(
                        "Conexão concluída",
                        "Este computador está conectado ao Bling.",
                        parent=self,
                    )
                elif tipo == "resultado":
                    self._definir_execucao(False)
                    self.ultimo_resultado = valor
                    resumo = (
                        f"NF-e encontradas: {valor.total_notas}\n"
                        f"XMLs baixados: {valor.baixados}\n"
                        f"XMLs que já existiam: {valor.existentes}\n"
                        f"Erros: {valor.erros + valor.erros_organizacao}"
                    )
                    if valor.arquivos_zip_gnre:
                        resumo += (
                            f"\nGNRE incluídas nos ZIPs: {valor.gnres_no_zip}\n"
                            "Arquivos: "
                            + ", ".join(
                                caminho.name
                                for caminho in valor.arquivos_zip_gnre
                            )
                        )
                    elif valor.somente_gnre_zip:
                        resumo += "\nNenhuma GNRE encontrada para essa janela."
                    self._registrar("Processamento finalizado.")
                    if valor.codigo_saida == 0:
                        messagebox.showinfo("Processamento concluído", resumo, parent=self)
                    else:
                        messagebox.showwarning(
                            "Processamento concluído com avisos", resumo, parent=self
                        )
                elif tipo == "erro":
                    self._definir_execucao(False)
                    self._registrar(valor)
                    messagebox.showerror("Erro", valor, parent=self)
        except queue.Empty:
            pass
        self.after(100, self._processar_fila)

    def _abrir_pasta(self):
        pasta = (
            self.ultimo_resultado.pasta_resultado
            if self.ultimo_resultado
            else DATA_DIR / "xml_por_cnpj"
        )
        pasta.mkdir(parents=True, exist_ok=True)
        os.startfile(pasta)

    def _ao_fechar(self):
        if self.em_execucao:
            messagebox.showwarning(
                "Processamento em andamento",
                "Aguarde o término antes de fechar o aplicativo.",
                parent=self,
            )
            return
        self.destroy()


def main():
    aplicacao = Aplicacao()
    if "--smoke-test" in sys.argv:
        aplicacao.update_idletasks()
        aplicacao.update()
        aplicacao.destroy()
        return
    aplicacao.mainloop()


if __name__ == "__main__":
    main()
