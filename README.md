# MQTT Web Simples

Painel Flask compacto para entrar no broker pela tela de configurações, acompanhar mensagens, publicar payloads, apagar mensagens retidas e testar uma notificação do Telegram.

## Credenciais e privacidade

- O código público não contém senha, host, usuário MQTT, token nem chat ID do Telegram.
- O painel pede autenticação HTTP Basic. Configure `WEB_USER` e `WEB_PASS` no ambiente do Render ou em um `.env` local.
- Host, usuário e senha MQTT e os dados do Telegram são preenchidos na tela **Conexão e configurações**. O servidor guarda esses dados fora do repositório em `MQTT_DATA_DIR/settings.json`, com permissão restrita ao usuário do processo. A senha e o token nunca são devolvidos pela API.
- A senha em branco mantém a senha já salva. Use o botão **Apagar todas as configurações salvas** para remover as credenciais.
- O formulário só deve ser usado por HTTPS quando estiver na internet. O TLS do broker valida o certificado por padrão.

## Teste local

Requer Python 3.10 ou superior.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edite `.env` e troque o usuário e a senha do painel. Inicie:

```bash
python app.py
```

Acesse `http://127.0.0.1:8080`, autentique-se com `WEB_USER` e `WEB_PASS` e configure o broker no painel. A porta padrão `8080` não interfere no serviço existente em `5050`.

Para um broker sem TLS, desmarque TLS na tela de configuração. As credenciais são salvas no arquivo local `data/settings.json`, fora do código e ignorado pelo Git.

## Hospedagem para teste

O GitHub guarda o código; ele não executa o servidor Flask pelo GitHub Pages. O botão abaixo usa o `render.yaml` para criar um serviço de teste gratuito no Render, com HTTPS. Como esse repositório é público e o botão pode ser usado por outras pessoas, o Blueprint começa com deploy automático desligado. Para sua própria instância atualizar a cada envio, conecte o serviço ao GitHub e escolha **Auto-Deploy → On Commit** no Render. Informe `WEB_USER` e `WEB_PASS` nos campos privados do Render. Depois, entre no painel e digite as credenciais MQTT e Telegram nas configurações.

[Iniciar hospedagem gratuita no Render](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2Fjesuspity%2Fmqtt-ex-web)

O plano gratuito pode suspender o serviço após inatividade e não preserva `settings.json` depois de suspensão ou reinício. Para manter as configurações e o processo MQTT ativos, use um plano pago com disco persistente montado em `/var/data` e defina `MQTT_DATA_DIR=/var/data`. Não há nenhuma cobrança criada por este repositório.

## Execução

Para o estado global do cliente MQTT, use um processo Gunicorn. O comando já limita a um worker, mantém threads para a interface e desativa preload para iniciar o MQTT no processo correto. O filtro inicial de tópicos é `#`; configure ACLs de broker com os tópicos mínimos necessários, pois o painel permite publicação e remoção de retained.
