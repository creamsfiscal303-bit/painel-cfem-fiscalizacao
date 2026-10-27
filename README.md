# Painel de Inteligência Fiscalizatória — CFEM/ANM

Aplicação Streamlit para explorar os dados públicos de arrecadação da CFEM da ANM em todo o Brasil. O usuário seleciona uma UF por vez para reduzir o consumo de memória.

## Conteúdo
- `app.py`: aplicação Streamlit
- `requirements.txt`: dependências
- `README.md`: instruções de publicação

## Fonte
Arquivo oficial ANM — CFEM Arrecadação 2022–2026:
https://dadosabertos.anm.gov.br/CFEM/CFEM_Arrecadacao_2022_2026.csv

O aplicativo baixa o arquivo sob demanda e lê o CSV em blocos, mantendo na memória apenas a UF selecionada. A base é atualizada pela ANM; confirme a versão e a data de publicação antes de usar os resultados.

## Publicar no Streamlit Community Cloud
1. Crie um repositório no GitHub, por exemplo `painel-cfem-fiscalizacao`.
2. Envie `app.py`, `requirements.txt` e `README.md` para a raiz do repositório.
3. Acesse https://share.streamlit.io/ e entre com sua conta GitHub.
4. Clique em **Create app**.
5. Selecione o repositório, a branch `main` e o arquivo `app.py`.
6. Clique em **Deploy**. Depois da publicação, copie o endereço `*.streamlit.app` para compartilhar com os agentes.

## Teste local (opcional)
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Observações
- A primeira abertura pode demorar, pois o arquivo oficial tem aproximadamente 100 MB.
- A aplicação depende de o servidor da ANM estar acessível e manter o caminho do arquivo.
- O painel não determina automaticamente infração nem comprova atividade atual. Use-o como ferramenta de triagem e confirme os dados em fontes oficiais antes de iniciar uma ação fiscalizatória.
- O painel não reconstrói CPFs mascarados.
