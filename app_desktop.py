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

from baixar_xmls import DATA_DIR, calcular_periodo, processar_download
from bling_auth import ENV_FILE, TOKENS_FILE
from organizar_xmls import CONFIG_FILE


COR_VERDE = "#087B45"
COR_FUNDO = "#F3F6F4"
COR_TEXTO = "#23372D"
COR_SUAVE = "#586B61"
COR_BORDA = "#DCE5DF"
COR_ERRO = "#B42318"

DESCRICOES_MODO = {
    "com_gnre": "Organiza os XMLs das NF-e e cria cópias adicionais para o fluxo de GNRE.",
    "sem_gnre": "Organiza os XMLs das NF-e por unidade, marketplace, data e UF.",
    "somente_gnre_zip": "Separa os XMLs destinados à GNRE em um ZIP por unidade. Não emite guias de pagamento.",
}


def ultima_janela_disponivel(agora=None, ate_agora=False):
    agora = agora or datetime.now()
    if ate_agora or agora.time() >= horario(14):
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
        self.geometry("1040x760")
        self.minsize(720, 660)
        self.configure(bg=COR_FUNDO)
        self.fila = queue.Queue()
        self.em_execucao = False
        self.ultimo_resultado = None

        self._configurar_estilo()
        self._montar_tela()
        self.data_var.trace_add("write", self._atualizar_periodo)
        self.ate_agora_var.trace_add("write", self._atualizar_periodo)
        self.modo_var.trace_add("write", self._atualizar_modo)
        self._atualizar_modo()
        self._atualizar_periodo()
        self._atualizar_estado_conexao()
        self.after(100, self._processar_fila)
        self.after(1000, self._renovar_periodo)
        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)

    def _configurar_estilo(self):
        estilo = ttk.Style(self)
        estilo.theme_use("clam")
        estilo.configure(".", font=("Segoe UI", 10), foreground=COR_TEXTO)
        estilo.configure("TFrame", background=COR_FUNDO)
        estilo.configure(
            "Card.TFrame", background="white", bordercolor=COR_BORDA,
            borderwidth=1, relief="solid",
        )
        estilo.configure("Plain.TFrame", background="white")
        estilo.configure("Soft.TFrame", background="#EDF5EF")
        estilo.configure("Metric.TFrame", background="#F4F7F5")
        estilo.configure(
            "Title.TLabel",
            background=COR_FUNDO,
            foreground=COR_TEXTO,
            font=("Segoe UI", 20, "bold"),
        )
        estilo.configure(
            "Subtitle.TLabel",
            background=COR_FUNDO,
            foreground=COR_SUAVE,
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
        estilo.configure("Hint.TLabel", background="white", foreground=COR_SUAVE)
        estilo.configure("Period.TLabel", background="#EDF5EF", foreground=COR_TEXTO)
        estilo.configure(
            "Metric.TLabel", background="#F4F7F5", foreground=COR_TEXTO,
            font=("Segoe UI", 18, "bold"),
        )
        estilo.configure("MetricCaption.TLabel", background="#F4F7F5", foreground=COR_SUAVE, font=("Segoe UI", 9))
        for estado, fundo, texto in (
            ("Neutro", "#EDF2EF", COR_SUAVE),
            ("Ativo", "#E9F1FA", "#205A96"),
            ("Sucesso", "#E6F4EA", "#17643A"),
            ("Aviso", "#FFF3DD", "#895509"),
            ("Erro", "#FDECE9", COR_ERRO),
        ):
            estilo.configure(
                f"{estado}.Badge.TLabel", background=fundo, foreground=texto,
                font=("Segoe UI", 9, "bold"), padding=(10, 5),
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
            background=[("disabled", "#CBD8D0"), ("pressed", "#075D35"), ("active", "#096A3E")],
            foreground=[("disabled", "#4F6358")],
        )
        estilo.configure("TButton", background="white", bordercolor=COR_BORDA, padding=(10, 7))
        estilo.configure("Secondary.TButton", padding=(12, 8))
        estilo.configure("Shortcut.TButton", padding=(10, 7))
        estilo.configure("Selected.Shortcut.TButton", background="#E6F4EA", foreground="#17643A", bordercolor=COR_VERDE)
        estilo.map("Selected.Shortcut.TButton", background=[("active", "#D5EBDD"), ("disabled", "#E6ECE8")])
        estilo.configure("TRadiobutton", background="white", foreground=COR_TEXTO, padding=(0, 4))
        estilo.map("TRadiobutton", background=[("active", "white")])
        estilo.configure("TEntry", padding=6, bordercolor=COR_BORDA)
        estilo.configure("Invalid.TEntry", bordercolor=COR_ERRO)
        estilo.configure("Accent.Horizontal.TProgressbar", background=COR_VERDE, troughcolor="#E6ECE8", borderwidth=0)

    def _montar_tela(self):
        tk.Frame(self, background=COR_VERDE, height=4).pack(fill="x")
        cabecalho = ttk.Frame(self, padding=(24, 16, 24, 12))
        cabecalho.pack(fill="x")
        ttk.Label(
            cabecalho, text="Automação XML Bling", style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            cabecalho,
            text="Escolha o período, baixe os XMLs e encontre seus arquivos.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        area = ttk.Frame(self)
        area.pack(fill="both", expand=True)
        self.canvas_conteudo = tk.Canvas(
            area, bg=COR_FUNDO, highlightthickness=0,
        )
        barra = ttk.Scrollbar(
            area, orient="vertical", command=self.canvas_conteudo.yview,
        )
        barra.pack(side="right", fill="y")
        self.canvas_conteudo.pack(side="left", fill="both", expand=True)
        self.canvas_conteudo.configure(yscrollcommand=barra.set)
        conteudo = ttk.Frame(self.canvas_conteudo, padding=(24, 0, 12, 20))
        janela = self.canvas_conteudo.create_window(
            (0, 0), window=conteudo, anchor="nw",
        )
        conteudo.bind("<Configure>", lambda evento: self.canvas_conteudo.configure(
            scrollregion=self.canvas_conteudo.bbox("all")
        ))
        self.janela_conteudo = janela
        self.canvas_conteudo.bind("<Configure>", self._ajustar_layout)
        self.bind("<MouseWheel>", self._rolar_conteudo)

        cartao = ttk.Frame(conteudo, style="Card.TFrame", padding=16)
        cartao.pack(fill="x")
        cartao.columnconfigure(0, weight=1)

        ttk.Label(cartao, text="Conexão com o Bling", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.label_conexao = ttk.Label(cartao, style="Neutro.Badge.TLabel")
        self.label_conexao.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.label_conexao_ajuda = ttk.Label(cartao, style="Hint.TLabel", wraplength=470)
        self.label_conexao_ajuda.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.label_conexao_ajuda.bind("<Configure>", lambda evento:
            self.label_conexao_ajuda.configure(wraplength=max(1, evento.width))
        )
        self.botao_conectar = ttk.Button(
            cartao,
            text="Conectar ao Bling",
            style="Secondary.TButton",
            command=self._conectar,
        )
        self.botao_conectar.grid(row=0, column=1, rowspan=3, sticky="e", padx=(16, 0))

        self.corpo = ttk.Frame(conteudo)
        self.corpo.pack(fill="both", expand=True, pady=(12, 0))
        opcoes = ttk.Frame(self.corpo, style="Card.TFrame", padding=16)
        self.painel_opcoes = opcoes
        opcoes.grid(row=0, column=0, sticky="new")
        opcoes.columnconfigure(1, weight=1)
        ttk.Label(opcoes, text="1. Escolha o período", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )

        self.data_var = tk.StringVar(
            value=ultima_janela_disponivel().strftime("%d/%m/%Y")
        )
        datas = ttk.Frame(opcoes, style="Plain.TFrame")
        datas.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.campo_data = ttk.Entry(
            datas, textvariable=self.data_var, width=12, font=("Segoe UI", 11)
        )
        self.campo_data.pack(side="left")
        self.campo_data.bind("<FocusOut>", lambda evento: self._atualizar_periodo())
        self.botao_ultima_janela = ttk.Button(
            datas, text="Última janela completa", style="Shortcut.TButton",
            command=lambda: self._selecionar_atalho(False),
        )
        self.botao_ultima_janela.pack(side="left", padx=(8, 4))
        self.botao_hoje = ttk.Button(
            datas, text="Hoje até agora", style="Shortcut.TButton",
            command=lambda: self._selecionar_atalho(True),
        )
        self.botao_hoje.pack(side="left")

        self.ate_agora_var = tk.BooleanVar(value=False)
        periodo = ttk.Frame(opcoes, style="Soft.TFrame", padding=(12, 10))
        periodo.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.label_periodo = ttk.Label(
            periodo,
            style="Period.TLabel",
            wraplength=620,
        )
        self.label_periodo.pack(fill="x")
        self.label_periodo.bind("<Configure>", lambda evento:
            self.label_periodo.configure(wraplength=max(1, evento.width))
        )

        ttk.Separator(opcoes).grid(row=3, column=0, columnspan=2, sticky="ew", pady=16)
        ttk.Label(opcoes, text="2. Escolha os arquivos", style="CardTitle.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w",
        )
        self.modo_var = tk.StringVar(value="com_gnre")
        radios = ttk.Frame(opcoes, style="Plain.TFrame")
        radios.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.opcoes_modo = []
        for modo, titulo in (
            ("com_gnre", "NF-e organizadas + pasta GNRE"),
            ("sem_gnre", "Somente NF-e organizadas"),
            ("somente_gnre_zip", "XMLs de GNRE em ZIP por unidade"),
        ):
            radio = ttk.Radiobutton(
                radios, text=titulo, variable=self.modo_var, value=modo,
            )
            radio.pack(anchor="w")
            self.opcoes_modo.append(radio)
        self.label_modo_ajuda = ttk.Label(
            opcoes, style="Hint.TLabel", wraplength=620,
        )
        self.label_modo_ajuda.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.label_modo_ajuda.bind("<Configure>", lambda evento:
            self.label_modo_ajuda.configure(wraplength=max(1, evento.width))
        )

        acoes = ttk.Frame(opcoes, style="Plain.TFrame")
        acoes.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self.botao_executar = ttk.Button(
            acoes,
            text="Baixar e organizar",
            style="Accent.TButton",
            command=self._executar,
        )
        self.botao_executar.pack(side="left")
        ttk.Label(
            acoes, text="Os atalhos selecionam o período.\nEste botão inicia a consulta.",
            style="Hint.TLabel",
        ).pack(side="left", padx=(16, 0))

        painel_log = ttk.Frame(self.corpo, style="Card.TFrame", padding=16)
        self.painel_resultado = painel_log
        painel_log.grid(row=1, column=0, sticky="new", pady=(12, 0))
        ttk.Label(painel_log, text="3. Acompanhe o resultado", style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        self.label_estado = ttk.Label(painel_log, text="Pronto", style="Neutro.Badge.TLabel")
        self.label_estado.pack(anchor="w", pady=(0, 6))
        self.progresso = ttk.Progressbar(
            painel_log, mode="indeterminate", style="Accent.Horizontal.TProgressbar",
        )
        self.log_texto = tk.Text(
            painel_log,
            height=8,
            wrap="word",
            state="disabled",
            bg="#F8FAF9",
            fg=COR_TEXTO,
            relief="flat",
            padx=10,
            pady=10,
            font=("Consolas", 9),
        )
        # O resumo permanece visível; os detalhes técnicos são opcionais.
        self.status_var = tk.StringVar(value="Escolha o período e os arquivos para iniciar.")
        self.label_status = ttk.Label(
            painel_log, textvariable=self.status_var, style="Hint.TLabel",
            wraplength=620,
        )
        self.label_status.pack(fill="x", pady=(0, 10))
        self.label_status.bind("<Configure>", lambda evento:
            self.label_status.configure(wraplength=max(1, evento.width))
        )
        metricas = ttk.Frame(painel_log, style="Plain.TFrame")
        metricas.pack(fill="x")
        self.metricas_vars = {}
        self.metricas_labels = {}
        self.metricas_frames = []
        for coluna, (chave, titulo) in enumerate((
            ("total_notas", "NF-e encontradas"),
            ("baixados", "XMLs baixados"),
            ("existentes", "Já existentes"),
            ("erros", "Erros"),
        )):
            metricas.columnconfigure(coluna, weight=1, uniform="metricas")
            variavel = tk.StringVar(value="—")
            self.metricas_vars[chave] = variavel
            metrica = ttk.Frame(metricas, style="Metric.TFrame", padding=(12, 8))
            metrica.grid(row=0, column=coluna, sticky="ew", padx=(0, 6 if coluna < 3 else 0))
            label = ttk.Label(metrica, textvariable=variavel, style="Metric.TLabel")
            label.pack(anchor="w")
            self.metricas_labels[chave] = label
            self.metricas_frames.append(metrica)
            ttk.Label(metrica, text=titulo, style="MetricCaption.TLabel").pack(anchor="w")
        self.resumo_var = tk.StringVar(value="Nenhuma execução concluída nesta sessão.")
        self.label_resumo = ttk.Label(
            painel_log, textvariable=self.resumo_var, style="Card.TLabel",
            wraplength=620,
        )
        self.label_resumo.pack(fill="x", pady=(10, 8))
        self.label_resumo.bind("<Configure>", lambda evento:
            self.label_resumo.configure(wraplength=max(1, evento.width))
        )
        resultado_acoes = ttk.Frame(painel_log, style="Plain.TFrame")
        resultado_acoes.pack(fill="x")
        self.botao_abrir = ttk.Button(
            resultado_acoes, text="Abrir pasta de resultado",
            style="Secondary.TButton", command=self._abrir_pasta,
        )
        self.botao_abrir.pack(side="left")
        self.botao_detalhes = ttk.Button(
            resultado_acoes, text="Ver detalhes", command=self._alternar_detalhes,
        )
        self.botao_detalhes.pack(side="right")
        self._atualizar_botao_pasta()

    def _ajustar_layout(self, evento):
        self.canvas_conteudo.itemconfigure(self.janela_conteudo, width=evento.width)
        largo = evento.width >= 960
        self.corpo.columnconfigure(0, weight=3 if largo else 1)
        self.corpo.columnconfigure(1, weight=2 if largo else 0)
        self.painel_opcoes.grid_configure(padx=(0, 12 if largo else 0))
        self.painel_resultado.grid_configure(
            row=0 if largo else 1, column=1 if largo else 0,
            pady=0 if largo else (12, 0),
        )
        colunas = 2 if largo else 4
        metricas = self.metricas_frames[0].master
        for coluna in range(4):
            metricas.columnconfigure(
                coluna, weight=1 if coluna < colunas else 0,
                uniform="metricas" if coluna < colunas else "",
            )
        for indice, quadro in enumerate(self.metricas_frames):
            quadro.grid_configure(
                row=indice // colunas, column=indice % colunas,
                padx=(0, 6 if indice % colunas < colunas - 1 else 0),
                pady=(0, 6 if largo and indice < 2 else 0),
            )

    def _atualizar_modo(self, *_):
        modo = self.modo_var.get()
        self.label_modo_ajuda.configure(text=DESCRICOES_MODO[modo])
        if not self.em_execucao:
            self.botao_executar.configure(
                text="Baixar e gerar ZIPs" if modo == "somente_gnre_zip" else "Baixar e organizar",
            )

    def _mostrar_status(self, mensagem, estado="Neutro"):
        self.status_var.set(mensagem)
        titulo = {
            "Neutro": "Pronto", "Ativo": "Em andamento", "Sucesso": "Concluído",
            "Aviso": "Com avisos", "Erro": "Falha",
        }[estado]
        self.label_estado.configure(text=titulo, style=f"{estado}.Badge.TLabel")

    def _atualizar_botao_pasta(self):
        pasta = self.ultimo_resultado.pasta_resultado if self.ultimo_resultado else DATA_DIR / "xml_por_cnpj"
        self.botao_abrir.configure(state="normal" if pasta.is_dir() else "disabled")

    def _rolar_conteudo(self, evento):
        if evento.widget == self.log_texto:
            return
        limites = self.canvas_conteudo.bbox("all")
        if limites and limites[3] > self.canvas_conteudo.winfo_height():
            self.canvas_conteudo.yview_scroll(-int(evento.delta / 120), "units")

    def _alternar_detalhes(self):
        if self.log_texto.winfo_manager():
            self.log_texto.pack_forget()
            self.botao_detalhes.configure(text="Ver detalhes")
        else:
            self.log_texto.pack(fill="both", expand=True, pady=(10, 0))
            self.botao_detalhes.configure(text="Ocultar detalhes")

    def _selecionar_atalho(self, ate_agora):
        if self.em_execucao:
            return
        self.ate_agora_var.set(ate_agora)
        self.data_var.set(
            ultima_janela_disponivel(ate_agora=ate_agora).strftime("%d/%m/%Y")
        )

    def _atualizar_periodo(self, *_):
        if self.em_execucao:
            return
        try:
            agora = datetime.now()
            selecionada = self._ler_data(agora)
            for botao, ate_agora in ((self.botao_ultima_janela, False), (self.botao_hoje, True)):
                selecionado = (
                    self.ate_agora_var.get() == ate_agora
                    and selecionada == ultima_janela_disponivel(agora, ate_agora)
                )
                botao.configure(style="Selected.Shortcut.TButton" if selecionado else "Shortcut.TButton")
            inicio, fim = calcular_periodo(
                selecionada, janela_14h=True,
                ate_agora=self.ate_agora_var.get(), agora=agora,
            )
            texto = (
                f"Período: de {inicio:%d/%m/%Y às %H:%M} "
                f"até {fim:%d/%m/%Y às %H:%M}.\n"
                + ("Desde as 14h de ontem até agora. O término é atualizado ao iniciar."
                   if self.ate_agora_var.get() else "Janela completa, encerrada às 14h.")
            )
            self.label_periodo.configure(text=texto, foreground=COR_TEXTO)
            self.campo_data.configure(style="TEntry")
            self.campo_data.configure(state="readonly" if self.ate_agora_var.get() else "normal")
        except (RuntimeError, OverflowError) as erro:
            self.label_periodo.configure(text=str(erro), foreground=COR_ERRO)
            self.campo_data.configure(style="Invalid.TEntry")
            self.botao_hoje.configure(style="Shortcut.TButton")
            self.botao_ultima_janela.configure(style="Shortcut.TButton")

    def _renovar_periodo(self):
        if not self.em_execucao and self.ate_agora_var.get():
            hoje = datetime.now().strftime("%d/%m/%Y")
            if self.data_var.get() != hoje:
                self.data_var.set(hoje)
        self._atualizar_periodo()
        self.after(1000, self._renovar_periodo)

    def _registrar(self, mensagem):
        self.log_texto.configure(state="normal")
        self.log_texto.insert("end", mensagem.rstrip() + "\n")
        self.log_texto.see("end")
        self.log_texto.configure(state="disabled")

    def _atualizar_estado_conexao(self):
        if TOKENS_FILE.is_file():
            self.label_conexao.configure(text="Autorização salva", style="Sucesso.Badge.TLabel")
            self.label_conexao_ajuda.configure(text="A conexão será verificada ao iniciar a consulta.")
            self.botao_conectar.configure(text="Reconectar")
        else:
            self.label_conexao.configure(text="Conexão necessária", style="Aviso.Badge.TLabel")
            self.label_conexao_ajuda.configure(text="Conecte sua conta do Bling para baixar os XMLs.")
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
        self.botao_ultima_janela.configure(state=estado)
        self.botao_hoje.configure(state=estado)
        for opcao in self.opcoes_modo:
            opcao.configure(state=estado)
        if ativa:
            self.botao_executar.configure(text="Aguarde…")
            self.progresso.pack(fill="x", before=self.label_status, pady=(0, 8))
            self.progresso.start(12)
        else:
            self.progresso.stop()
            self.progresso.pack_forget()
            self._atualizar_modo()
            self._atualizar_estado_conexao()
            self._atualizar_periodo()

    def _conectar(self):
        try:
            self._validar_arquivos(exigir_token=False)
        except RuntimeError as erro:
            messagebox.showerror("Configuração necessária", str(erro), parent=self)
            return
        self._definir_execucao(True)
        self.botao_conectar.configure(text="Conectando…")
        self._mostrar_status("Autorize o acesso à sua conta no navegador.", "Ativo")
        self._registrar("Abrindo a autorização do Bling no navegador...")
        threading.Thread(target=self._trabalho_autenticacao, daemon=True).start()

    def _trabalho_autenticacao(self):
        try:
            from autenticar_bling import main as autenticar

            autenticar()
            self.fila.put(("autenticacao_ok", None))
        except Exception as erro:
            self.fila.put(("erro", f"Não foi possível conectar ao Bling: {erro}"))

    def _ler_data(self, agora=None):
        try:
            selecionada = datetime.strptime(self.data_var.get().strip(), "%d/%m/%Y").date()
        except ValueError as erro:
            raise RuntimeError("Informe a data no formato DD/MM/AAAA.") from erro
        limite = ultima_janela_disponivel(agora, ate_agora=self.ate_agora_var.get())
        if selecionada > limite:
            if self.ate_agora_var.get():
                raise RuntimeError("Selecione uma data até hoje para consultar até agora.")
            raise RuntimeError(
                "Essa janela ainda não terminou. Selecione uma data até "
                f"{limite.strftime('%d/%m/%Y')} ou clique em Hoje até agora para consultar hoje."
            )
        if self.ate_agora_var.get() and selecionada != (agora or datetime.now()).date():
            raise RuntimeError("Hoje até agora exige a data de hoje. Use Última janela completa para escolher outra data.")
        return selecionada

    def _executar(self):
        try:
            self._validar_arquivos()
            data_selecionada = self._ler_data()
        except RuntimeError as erro:
            messagebox.showerror("Não foi possível iniciar", str(erro), parent=self)
            return

        self._atualizar_periodo()
        self._definir_execucao(True)
        self._mostrar_status(
            f"Consultando e organizando os XMLs da janela de {data_selecionada:%d/%m/%Y}…",
            "Ativo",
        )
        self._registrar(
            f"Iniciando a janela {data_selecionada.strftime('%d/%m/%Y')}..."
        )
        modo = self.modo_var.get()
        ate_agora = self.ate_agora_var.get()
        incluir_gnre = modo == "com_gnre"
        somente_gnre_zip = modo == "somente_gnre_zip"
        threading.Thread(
            target=self._trabalho_download,
            args=(data_selecionada, incluir_gnre, somente_gnre_zip, ate_agora),
            daemon=True,
        ).start()

    def _trabalho_download(
        self,
        data_selecionada,
        incluir_gnre,
        somente_gnre_zip,
        ate_agora,
    ):
        try:
            handler = HandlerFila(self.fila)
            resultado = processar_download(
                data_selecionada,
                janela_14h=True,
                ate_agora=ate_agora,
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
                    self._mostrar_status("Autorização concluída. Escolha o período e inicie a consulta.")
                    messagebox.showinfo(
                        "Conexão concluída",
                        "Este computador está conectado ao Bling.",
                        parent=self,
                    )
                elif tipo == "resultado":
                    self._definir_execucao(False)
                    self.ultimo_resultado = valor
                    for chave in ("total_notas", "baixados", "existentes"):
                        self.metricas_vars[chave].set(str(getattr(valor, chave)))
                    self.metricas_vars["erros"].set(str(valor.erros + valor.erros_organizacao))
                    self.metricas_labels["erros"].configure(
                        foreground=COR_ERRO if valor.erros + valor.erros_organizacao else COR_TEXTO,
                    )
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
                    detalhes = f"Último resultado: janela de {valor.data_consulta:%d/%m/%Y}."
                    if valor.sem_chave:
                        detalhes += f"\nNotas sem chave de acesso: {valor.sem_chave}."
                    if valor.arquivos_zip_gnre:
                        detalhes += (
                            f"\nXMLs de GNRE nos ZIPs: {valor.gnres_no_zip}. Arquivos: "
                            + ", ".join(caminho.name for caminho in valor.arquivos_zip_gnre)
                        )
                    elif valor.somente_gnre_zip:
                        detalhes += "\nNenhuma GNRE encontrada para essa janela."
                    self.resumo_var.set(detalhes)
                    self._atualizar_botao_pasta()
                    self._registrar("Processamento finalizado.")
                    self._registrar(resumo)
                    if valor.codigo_saida == 0:
                        self._mostrar_status(
                            "Nenhuma NF-e encontrada neste período. Escolha outra data se necessário."
                            if not valor.total_notas else "Consulta finalizada. Confira o resumo abaixo.",
                            "Sucesso",
                        )
                    else:
                        self._mostrar_status("Alguns itens precisam de atenção. Abra os detalhes para conferir.", "Aviso")
                elif tipo == "erro":
                    self._definir_execucao(False)
                    self._registrar(valor)
                    self._mostrar_status(valor, "Erro")
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
        try:
            if not pasta.is_dir():
                raise OSError("A pasta de resultado não está disponível. Execute uma consulta para gerar os arquivos.")
            os.startfile(pasta)
        except OSError as erro:
            self._atualizar_botao_pasta()
            messagebox.showerror("Não foi possível abrir a pasta", str(erro), parent=self)

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
