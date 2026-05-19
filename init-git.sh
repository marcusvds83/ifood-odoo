#!/bin/bash
# ============================================
# Script para inicializar e enviar código ao GitHub
# Repositório: marcusvds83/ifood-odoo
# ============================================

cd "$(dirname "$0")"

echo "=== Inicializando repositório Git ==="

# Inicializar git
git init
git branch -M main

# Adicionar todos os arquivos (exceto .env com credenciais)
echo "=== Adicionando arquivos ==="
git add .gitignore
git add README.md
git add Dockerfile
git add render.yaml
git add requirements.txt
git add app/
git add odoo_module/
git add tests/
git add scripts/

# Commit inicial
echo "=== Criando commit ==="
git commit -m "feat: iFood-Odoo middleware v1.0

- FastAPI middleware com OAuth 2.0 iFood
- Webhook para receber pedidos em tempo real
- Integração Odoo via XML-RPC
- 26 campos customizados no módulo Odoo
- Views XML para sale.order, partner, product
- Dockerfile pronto para deploy no Render"

# Adicionar remote e push
echo "=== Conectando ao GitHub ==="
git remote add origin https://github.com/marcusvds83/ifood-odoo.git

echo ""
echo "=== Pronto! Agora execute o push: ==="
echo ""
echo "  git push -u origin main"
echo ""
echo "⚠️  IMPORTANTE: O GitHub vai pedir sua autenticação."
echo "    Use seu Personal Access Token (PAT) como senha."
echo "    Gere um em: GitHub > Settings > Developer settings > Personal access tokens"
echo ""
