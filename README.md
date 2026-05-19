# iFood-Odoo Middleware

Middleware que integra pedidos do iFood com o Odoo ERP. Recebe webhooks do iFood, sincroniza automaticamente pedidos, clientes e produtos com o Odoo, e permite gerenciar o ciclo de vida dos pedidos.

## Funcionalidades

- **Autenticação OAuth 2.0** com a API do iFood (client_credentials)
- **Recebimento de Webhooks** do iFood (pedidos, mudanças de status, catálogo)
- **Sincronização automática** de pedidos para sale.order no Odoo
- **Criação automática** de clientes (res.partner) e produtos (product.product)
- **Gerenciamento de status** dos pedidos (confirmar, iniciar preparo, pronto, despachar)
- **Health checks** para iFood e Odoo
- **API REST** para consulta e gestão manual de pedidos

## Requisitos

- Python 3.12+
- Odoo 16 com o módulo de integração instalado
- Credenciais da API iFood (clientId e clientSecret)

## Instalação

```bash
# Clone o repositório
cd ifood-odoo-middleware

# Crie e ative um ambiente virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# Instale as dependências
pip install -r requirements.txt

# Configure o arquivo .env
cp .env.example .env
# Edite o .env com suas credenciais
```

## Configuração

Edite o arquivo `.env` com suas credenciais:

```env
# iFood OAuth 2.0
IFOOD_CLIENT_ID=seu_client_id
IFOOD_CLIENT_SECRET=seu_client_secret

# Odoo Connection
ODOO_URL=https://seu-odoo.com
ODOO_DB=nome_do_banco
ODOO_USER=seu_usuario
ODOO_PASSWORD=sua_senha_api
```

### Instalação do Módulo Odoo

Copie a pasta `odoo_module/` para o diretório de addons do seu Odoo e instale o módulo **"iFood Integration"**.

## Execução

### Desenvolvimento

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 10000
```

### Produção com Docker

```bash
docker build -t ifood-odoo-middleware .
docker run -p 10000:10000 --env-file .env ifood-odoo-middleware
```

## Estrutura do Projeto

```
ifood-odoo-middleware/
├── app/
│   ├── __init__.py
│   ├── config.py              # Configurações (Pydantic Settings)
│   ├── main.py                # Entry point FastAPI
│   ├── models/
│   │   └── ifood_models.py    # Modelos Pydantic para dados do iFood
│   ├── routes/
│   │   ├── health.py          # Health checks
│   │   ├── ifood_webhooks.py  # Webhooks do iFood
│   │   └── orders.py          # Gestão de pedidos
│   ├── services/
│   │   ├── ifood_auth.py      # Autenticação OAuth iFood
│   │   ├── ifood_api.py       # Cliente API iFood
│   │   ├── odoo_client.py     # Cliente XML-RPC Odoo
│   │   └── odoo_sync.py       # Serviço de sincronização
│   └── utils/
│       └── logger.py          # Utilitário de logging
├── odoo_module/               # Módulo Odoo
│   ├── __manifest__.py
│   ├── models/
│   ├── security/
│   └── views/
├── .env
├── .gitignore
├── Dockerfile
├── render.yaml
├── requirements.txt
└── README.md
```

## Endpoints da API

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/health` | Health check geral |
| GET | `/health/ifood` | Testa conectividade com iFood |
| GET | `/health/odoo` | Testa conectividade com Odoo |
| POST | `/webhooks/ifood/orders` | Webhook de pedidos iFood |
| POST | `/webhooks/ifood/catalog` | Webhook de catálogo iFood |
| GET | `/orders` | Lista pedidos do iFood no Odoo |
| GET | `/orders/{order_id}` | Detalhes de um pedido |
| POST | `/orders/sync/{ifood_order_id}` | Sincroniza pedido manualmente |
| POST | `/orders/{ifood_order_id}/confirm` | Confirma pedido no iFood |
| POST | `/orders/{ifood_order_id}/start-preparation` | Inicia preparo |
| POST | `/orders/{ifood_order_id}/ready` | Marca pronto para retirada |
| POST | `/orders/{ifood_order_id}/dispatch` | Despacha pedido |

## Deploy no Render

O arquivo `render.yaml` está configurado para deploy direto. Basta:

1. Conectar o repositório ao Render
2. Configurar as variáveis de ambiente no painel
3. O deploy será feito automaticamente

## Licença

MIT
