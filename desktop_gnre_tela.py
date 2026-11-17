"""Prévia e acompanhamento GNRE dentro do aplicativo desktop."""

from datetime import datetime
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from desktop_gnre_cliente import ClienteGNRE, ler_arquivos, revalidar_bling, salvar_pdf

TITULOS = {'preparando': 'Preparando prévia', 'pronta': 'Pronta para seleção', 'fora_piloto': 'Fora do piloto',
           'falha_preparo': 'Não preparada', 'entrada_invalida': 'Documento fora do escopo',
           'gerada': 'Gerada', 'aguardando_confirmacao': 'Aguardando a SEFAZ',
           'em_revisao': 'Precisa de conferência', 'parcial': 'Parcialmente gerada',
           'falha_sem_emissao': 'Rejeitada, sem emissão', 'falha_antes_envio': 'Envio bloqueado',
           'aguardando_decisao': 'Aguardando decisão', 'existente': 'Já registrada',
           'encaminhada_portal': 'Encaminhada ao portal'}


def data_interface(texto):
    try:
        return datetime.strptime(texto.strip(), '%d/%m/%Y').date().isoformat()
    except ValueError as erro:
        raise RuntimeError('Informe a data no formato DD/MM/AAAA.') from erro


class JanelaGNRE(tk.Toplevel):
    def __init__(self, pai, pasta_dados, arquivos=()):
        super().__init__(pai)
        self.title('Guias GNRE — prévia e acompanhamento')
        self.geometry('1120x760')
        self.minsize(860, 620)
        self.pasta_dados = Path(pasta_dados) / 'Guias-GNRE'
        self.pasta_dados.mkdir(parents=True, exist_ok=True)
        self.estado_local = self.pasta_dados / 'ultimo-pedido.json'
        self.config_local = self.pasta_dados / 'acesso.json'
        self.cliente = None
        self.pedido = None
        self.arquivos = list(map(str, arquivos))
        self.ocupada = False
        self.fila = queue.Queue()
        self.linhas = {}
        self._fila_id = self._automatico_id = self._retomar_id = None
        self._montar()
        self._fila_id = self.after(150, self._fila)
        self._automatico_id = self.after(6000, self._automatico)
        caminho = os.getenv('GNRE_DESKTOP_CONFIG')
        if not caminho and self.config_local.exists():
            caminho = json.loads(self.config_local.read_text()).get('configuracao')
        if not caminho:
            candidato = Path(__file__).resolve().parent / 'segredos/desktop-gnre/configuracao.json'
            if candidato.is_file():
                caminho = str(candidato)
        if caminho:
            self._carregar_acesso(caminho)

    def _montar(self):
        corpo = ttk.Frame(self, padding=16)
        corpo.pack(fill='both', expand=True)
        ttk.Label(corpo, text='Guias GNRE', font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(corpo, text='Prepare e confira as notas antes de confirmar uma emissão oficial.').pack(anchor='w', pady=(3, 12))
        topo = ttk.Frame(corpo)
        topo.pack(fill='x')
        ttk.Button(topo, text='Configurar acesso ao Debian', command=self._configurar).pack(side='left')
        self.conexao = tk.StringVar(value='Acesso ainda não configurado neste computador.')
        ttk.Label(topo, textvariable=self.conexao).pack(side='left', padx=12)
        selecao = ttk.LabelFrame(corpo, text='1. Selecione as notas e confira as datas', padding=12)
        selecao.pack(fill='x', pady=12)
        ttk.Button(selecao, text='Selecionar XMLs ou ZIPs', command=self._escolher).grid(row=0, column=0, sticky='w')
        self.selecao = tk.StringVar(value=f'{len(self.arquivos)} arquivo(s) selecionado(s).')
        ttk.Label(selecao, textvariable=self.selecao).grid(row=0, column=1, columnspan=3, sticky='w', padx=12)
        ttk.Label(selecao, text='Pagamento previsto:').grid(row=1, column=0, sticky='w', pady=(12, 0))
        self.pagamento = tk.StringVar(value=datetime.now().strftime('%d/%m/%Y'))
        ttk.Entry(selecao, textvariable=self.pagamento, width=13).grid(row=1, column=1, sticky='w', padx=12, pady=(12, 0))
        ttk.Label(selecao, text='Vencimento específico (opcional):').grid(row=1, column=2, sticky='w', pady=(12, 0))
        self.vencimento = tk.StringVar()
        ttk.Entry(selecao, textvariable=self.vencimento, width=13).grid(row=1, column=3, sticky='w', padx=12, pady=(12, 0))
        ttk.Label(selecao, text='Sem data específica, a prévia usa emissão/saída do XML, com zero dias adicionais. Confira cada nota.').grid(row=2, column=0, columnspan=4, sticky='w', pady=(10, 0))
        self.botao_preparar = ttk.Button(selecao, text='Preparar prévia — sem emitir', command=self._preparar)
        self.botao_preparar.grid(row=3, column=0, columnspan=4, sticky='w', pady=(12, 0))
        quadro = ttk.LabelFrame(corpo, text='2. Prévia e acompanhamento por nota e imposto', padding=10)
        quadro.pack(fill='both', expand=True)
        colunas = ('unidade', 'nota', 'uf', 'imposto', 'valor', 'vencimento', 'pagamento', 'estado')
        self.tabela = ttk.Treeview(quadro, columns=colunas, show='headings', selectmode='extended', height=10)
        for nome, titulo, largura in zip(colunas, ('CNPJ', 'NF-e', 'UF', 'Imposto', 'R$', 'Vencimento', 'Pag. previsto', 'Situação'),
                                       (145, 65, 40, 70, 80, 95, 95, 195)):
            self.tabela.heading(nome, text=titulo)
            self.tabela.column(nome, width=largura, minwidth=40, stretch=nome == 'estado')
        vertical = ttk.Scrollbar(quadro, orient='vertical', command=self.tabela.yview)
        self.tabela.configure(yscrollcommand=vertical.set)
        vertical.pack(side='right', fill='y')
        self.tabela.pack(fill='both', expand=True)
        self.tabela.bind('<<TreeviewSelect>>', self._detalhe)
        self.detalhe = tk.StringVar(value='Selecione uma linha para ver o motivo e a orientação. Use Ctrl para selecionar mais notas.')
        ttk.Label(quadro, textvariable=self.detalhe, wraplength=1020).pack(fill='x', pady=(8, 0))
        botoes = ttk.Frame(corpo)
        botoes.pack(fill='x', pady=(12, 0))
        self.botao_emitir = ttk.Button(botoes, text='Emitir selecionadas', command=self._confirmar, state='disabled')
        self.botao_emitir.pack(side='left')
        self.botao_retomar = ttk.Button(botoes, text='Retomar acompanhamento', command=self._retomar)
        self.botao_retomar.pack(side='left', padx=6)
        self.botao_pdf = ttk.Button(botoes, text='Baixar PDFs', command=self._baixar)
        self.botao_pdf.pack(side='left')
        self.botao_manual = ttk.Button(botoes, text='XMLs para o portal', command=self._manual)
        self.botao_manual.pack(side='left', padx=6)
        self.estado = tk.StringVar(value='Prepare a prévia. Nenhuma guia será emitida nesta etapa.')
        ttk.Label(corpo, textvariable=self.estado, wraplength=1040).pack(fill='x', pady=(12, 0))
        ttk.Label(corpo, text='Guias geradas não significam pagamento confirmado. Pendências e emissões parciais exigem conferência.').pack(anchor='w', pady=(8, 0))

    def _configurar(self):
        caminho = filedialog.askopenfilename(parent=self, title='Configuração de acesso GNRE deste computador', filetypes=[('Configuração JSON', '*.json')])
        if caminho:
            self._carregar_acesso(caminho)

    def _carregar_acesso(self, caminho):
        try:
            self.cliente = ClienteGNRE(caminho)
            self.config_local.write_text(json.dumps({'configuracao': str(Path(caminho).resolve())}))
            self._iniciar(lambda: ('conexao', self.cliente.pedir('conectar')), 'Verificando acesso ao Debian…')
        except Exception as erro:
            self.conexao.set('Acesso precisa de configuração.')
            messagebox.showerror('Acesso GNRE', str(erro), parent=self)

    def _escolher(self):
        arquivos = filedialog.askopenfilenames(parent=self, title='Selecione os XMLs ou ZIPs para a prévia', filetypes=[('XMLs e ZIPs', '*.xml *.zip')])
        if arquivos:
            self.arquivos = list(arquivos)
            self.selecao.set(f'{len(arquivos)} arquivo(s) selecionado(s).')

    def _iniciar(self, funcao, mensagem):
        if self.ocupada:
            return
        self.ocupada = True
        self.estado.set(mensagem)
        self._botoes()
        def executar():
            try:
                self.fila.put(('sucesso', funcao()))
            except Exception as erro:
                self.fila.put(('erro', str(erro)))
        threading.Thread(target=executar, daemon=True).start()

    def _preparar(self):
        if not self.cliente:
            messagebox.showinfo('Acesso necessário', 'Configure o acesso GNRE deste computador.', parent=self)
            return
        try:
            pagamento = data_interface(self.pagamento.get())
            vencimento = data_interface(self.vencimento.get()) if self.vencimento.get().strip() else None
            arquivos = list(self.arquivos)
            if not arquivos:
                raise RuntimeError('Selecione os XMLs ou ZIPs da consulta.')
            pedido = uuid.uuid4().hex
            self.estado_local.write_text(json.dumps({'pedido': pedido}))
            self.pedido = {'id': pedido, 'notas': [], 'estado': 'preparando'}
            self._iniciar(lambda: ('pedido', self.cliente.pedir('preparar', pedido=pedido,
                pagamento=pagamento, vencimento=vencimento, arquivos=ler_arquivos(arquivos))), 'Preparando a prévia no Debian…')
        except Exception as erro:
            messagebox.showerror('Prévia GNRE', str(erro), parent=self)

    def _selecionadas(self):
        ids = {self.linhas[i] for i in self.tabela.selection()}
        return [n for n in (self.pedido or {}).get('notas', []) if n['id'] in ids]

    def _confirmar(self):
        notas = self._selecionadas()
        if not notas or not all(n.get('pode_emitir') for n in notas):
            messagebox.showinfo('Emissão ainda bloqueada', 'Selecione somente notas prontas e autorizadas para este piloto. Confira o motivo nas linhas.', parent=self)
            return
        from decimal import Decimal
        total = sum(Decimal(v) for n in notas for v in n['impostos'].values())
        texto = f"EMISSÃO OFICIAL\n\n{len(notas)} nota(s), valor principal total R$ {total:.2f}.\n"
        texto += '\n'.join(f"NF-e {n['numero']} / {n['uf']} / venc. {n['vencimento']} / pagamento {n['pagamento']}" for n in notas)
        texto += '\n\nConfirma que essas notas estão fora do envio ao GuiasGNRE enquanto a automação as processa?'
        if not messagebox.askyesno('Confirmar emissão oficial', texto, parent=self):
            return
        pedido, digest = self.pedido['id'], self.pedido['hash_previa']
        self._iniciar(lambda: ('pedido', self.cliente.pedir('confirmar', pedido=pedido, hash_previa=digest,
            notas=[n['id'] for n in notas], verificacoes_bling=revalidar_bling(notas))), 'Revalidando as notas no Bling e solicitando a emissão…')

    def _retomar(self, automatico=False):
        if not self.cliente or self.ocupada:
            return
        if not self.pedido and self.estado_local.exists():
            self.pedido = {'id': json.loads(self.estado_local.read_text())['pedido'], 'notas': []}
        if not self.pedido:
            if not automatico:
                self.estado.set('Nenhum pedido anterior neste computador.')
            return
        pedido = self.pedido['id']
        self._iniciar(lambda: ('pedido', self.cliente.pedir('consultar', pedido=pedido)), 'Consultando o mesmo pedido…')

    def _baixar(self):
        notas = [n for n in self._selecionadas() if n.get('pdf_disponivel')]
        if not notas:
            messagebox.showinfo('PDFs', 'Selecione notas com PDF conferido disponível.', parent=self)
            return
        destino = filedialog.askdirectory(parent=self, title='Salvar PDFs das guias')
        if destino:
            pedido = self.pedido['id']
            self._iniciar(lambda: ('arquivos', [str(salvar_pdf(self.cliente.pedir('pdf', pedido=pedido, nota=n['id']), destino)) for n in notas]), 'Conferindo e baixando os PDFs…')

    def _manual(self):
        notas = self._selecionadas()
        if not notas or not all(n.get('pode_encaminhar') or n['estado'] == 'encaminhada_portal' for n in notas):
            messagebox.showinfo('Tratamento no portal', 'Selecione somente falhas confirmadas sem guia emitida. Pendências, revisões e emissões parciais ficam fora do ZIP.', parent=self)
            return
        if not messagebox.askyesno('Encaminhar ao portal', 'Reservar estas notas para tratamento manual no GuiasGNRE? A automação bloqueará novas emissões delas.', parent=self):
            return
        destino = filedialog.askdirectory(parent=self, title='Salvar XMLs para o portal')
        if not destino:
            return
        pedido = self.pedido['id']
        def exportar():
            resposta = self.cliente.pedir('encaminhar_manual', pedido=pedido, notas=[n['id'] for n in notas],
                                         verificacoes_bling=revalidar_bling(notas))
            if not re.fullmatch(r'XMLs-GNRE-Portal-[a-f0-9]{8}\.zip', resposta['nome']):
                raise RuntimeError('Nome de ZIP inválido.')
            dados = base64.b64decode(resposta['conteudo'], validate=True)
            if hashlib.sha256(dados).hexdigest() != resposta['sha256']:
                raise RuntimeError('ZIP com integridade inválida.')
            arquivo = Path(destino) / resposta['nome']
            if arquivo.exists() and arquivo.read_bytes() != dados:
                raise RuntimeError('Existe outro ZIP com esse nome. Escolha outra pasta.')
            arquivo.write_bytes(dados)
            return ('arquivos', [str(arquivo)])
        self._iniciar(exportar, 'Conferindo e reservando os XMLs para o portal…')

    def _detalhe(self, *_):
        notas = self._selecionadas()
        self.detalhe.set('\n'.join(dict.fromkeys(n.get('bloqueio') or n.get('motivo', '') for n in notas)) if notas else 'Selecione uma linha para ver o motivo.')
        self._botoes()

    def _botoes(self):
        livre = not self.ocupada
        for botao in (self.botao_preparar, self.botao_retomar, self.botao_pdf, self.botao_manual):
            botao.configure(state='normal' if livre else 'disabled')
        notas = self._selecionadas()
        self.botao_emitir.configure(state='normal' if livre and notas and all(n.get('pode_emitir') for n in notas) else 'disabled')

    def _renderizar(self, pedido):
        selecionadas = {self.linhas[i] for i in self.tabela.selection()}
        self.pedido = pedido
        self.tabela.delete(*self.tabela.get_children())
        self.linhas.clear()
        for nota in pedido['notas']:
            impostos = list(nota.get('impostos', {}).items()) or [('—', '—')]
            guias = nota.get('guias', [])
            for indice, (imposto, valor) in enumerate(impostos):
                estado = TITULOS.get(nota['estado'], nota['estado'])
                receita = {'DIFAL': '100102', 'FCP': '100129'}.get(imposto)
                guia = next((g for g in guias if g.get('receita') == receita or
                    (g.get('origem', '').split('|')[2:3] == [receita])), None)
                if guia:
                    estado = guia['titulo']
                linha = self.tabela.insert('', 'end', values=(nota.get('cnpj', '—'), nota.get('numero', nota['nome']),
                    nota.get('uf', '—'), imposto, valor, nota.get('vencimento', '—'), nota.get('pagamento', '—'), estado))
                self.linhas[linha] = nota['id']
                if nota['id'] in selecionadas:
                    self.tabela.selection_add(linha)
        total = len(pedido['notas'])
        disponiveis = sum(bool(n.get('pode_emitir')) for n in pedido['notas'])
        self.estado.set(f"Pedido {pedido['id'][:8]} · {total} nota(s) · {disponiveis} liberada(s) para emissão · "
                        + ('preparando…' if pedido['estado'] == 'preparando' else 'selecione as linhas para conferir motivos e ações.'))
        self._botoes()

    def _fila(self):
        try:
            while True:
                estado, resultado = self.fila.get_nowait()
                self.ocupada = False
                if estado == 'erro':
                    self.estado.set(resultado)
                    self._botoes()
                else:
                    tipo, valor = resultado
                    if tipo == 'conexao':
                        self.conexao.set('Debian conectado · produção exige autorização por lote.')
                        self.estado.set(valor['mensagem'])
                        self._retomar_id = self.after(200, self._retomar)
                    elif tipo == 'pedido':
                        self._renderizar(valor)
                    elif tipo == 'arquivos':
                        self.estado.set('Arquivos salvos: ' + ', '.join(valor))
                        self._retomar_id = self.after(1000, self._retomar)
                    self._botoes()
        except queue.Empty:
            pass
        self._fila_id = self.after(150, self._fila)

    def _automatico(self):
        if self.pedido and self.pedido.get('estado') in ('preparando', 'envio_solicitado', 'acompanhando'):
            self._retomar(automatico=True)
        self._automatico_id = self.after(6000, self._automatico)

    def destroy(self):
        for campo in ('_fila_id', '_automatico_id', '_retomar_id'):
            ident = getattr(self, campo, None)
            if ident:
                try:
                    self.after_cancel(ident)
                except tk.TclError:
                    pass
        super().destroy()
