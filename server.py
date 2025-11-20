#server.py
from flask import Flask, request, jsonify, render_template_string, session, url_for, redirect
from flask_cors import CORS
import asyncio
import os
from datetime import datetime
from threading import Lock
import sys
import io
import pickle
import re

from pathlib import Path
import uuid

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from autogen_core import AgentId, SingleThreadedAgentRuntime
import main

app = Flask(__name__)

secret_key = os.environ.get('SECRET_KEY')
if not secret_key:
    raise RuntimeError("SECRET_KEY environment variable must be set for production security.")
app.secret_key = secret_key

app.config.update(
    SESSION_COOKIE_SECURE=True,  # Solo HTTPS en producción
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None',  # Permite cookies cross-site
    SESSION_COOKIE_DOMAIN='.onrender.com'  # Para Render
)

CORS(app, origins=[
    "https://texmax25.github.io",  # Cambia esto
    "http://localhost:5000"  # Para desarrollo local
], supports_credentials=True)

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/spreadsheets'
]

# Directorio para almacenar tokens de usuarios
TOKENS_DIR = Path('user_tokens')
TOKENS_DIR.mkdir(exist_ok=True)

# Variables globales
runtime = None
sheets_service = None
calendar_service = None
runtime_lock = Lock()

_sheets_service_cache = None
_calendar_service_cache = None

# HTML Templates (simplificados para evitar duplicación)
HTML_TEMPLATE = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Asistente de Gestión de Pagos</title><style>*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}.chat-container{width:100%;max-width:900px;height:90vh;background:white;border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,0.3);display:flex;flex-direction:column;overflow:hidden}.chat-header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:20px;text-align:center}.chat-header h1{font-size:24px;margin-bottom:5px}.chat-header p{font-size:14px;opacity:0.9}.connection-status{padding:10px;text-align:center;font-size:12px;background:#fff3cd;color:#856404}.connection-status.connected{background:#d4edda;color:#155724}.connection-status.error{background:#f8d7da;color:#721c24}.chat-messages{flex:1;overflow-y:auto;padding:20px;background:#f5f7fb}.message{margin-bottom:15px;display:flex;animation:fadeIn 0.3s ease-in}@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}.message.user{justify-content:flex-end}.message.bot{justify-content:flex-start}.message-content{max-width:75%;padding:14px 18px;border-radius:18px;word-wrap:break-word;white-space:pre-wrap;font-size:14px;line-height:1.6}.message.user .message-content{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;border-bottom-right-radius:4px}.message.bot .message-content{background:white;color:#333;border-bottom-left-radius:4px;box-shadow:0 2px 5px rgba(0,0,0,0.1)}.sheets-link{display:inline-block;margin-top:10px;padding:8px 15px;background:#4285f4;color:white;text-decoration:none;border-radius:20px;font-size:12px;transition:all 0.3s}.sheets-link:hover{background:#3367d6;transform:translateY(-2px)}.typing-indicator{display:none;padding:12px 18px;background:white;border-radius:18px;width:fit-content;box-shadow:0 2px 5px rgba(0,0,0,0.1)}.typing-indicator.active{display:block}.typing-indicator span{height:8px;width:8px;background:#667eea;border-radius:50%;display:inline-block;margin-right:5px;animation:typing 1.4s infinite}.typing-indicator span:nth-child(2){animation-delay:0.2s}.typing-indicator span:nth-child(3){animation-delay:0.4s}@keyframes typing{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-10px)}}.quick-actions{padding:15px 20px;background:white;border-top:1px solid #e0e0e0;display:flex;gap:10px;overflow-x:auto}.quick-btn{padding:8px 16px;background:#f0f0f0;border:none;border-radius:20px;cursor:pointer;white-space:nowrap;font-size:13px;transition:all 0.3s}.quick-btn:hover{background:#667eea;color:white;transform:translateY(-2px)}.chat-input-container{padding:20px;background:white;border-top:1px solid #e0e0e0}.chat-input-wrapper{display:flex;gap:10px;align-items:center}.chat-input{flex:1;padding:12px 18px;border:2px solid #e0e0e0;border-radius:25px;font-size:14px;outline:none;transition:border-color 0.3s}.chat-input:focus{border-color:#667eea}.chat-input:disabled{background:#f5f5f5;cursor:not-allowed;color:#999}.send-btn{width:50px;height:50px;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);border:none;border-radius:50%;color:white;font-size:20px;cursor:pointer;transition:transform 0.2s}.send-btn:hover{transform:scale(1.1)}.send-btn:active{transform:scale(0.95)}.send-btn:disabled{opacity:0.5;cursor:not-allowed}.chat-messages::-webkit-scrollbar{width:6px}.chat-messages::-webkit-scrollbar-track{background:#f1f1f1}.chat-messages::-webkit-scrollbar-thumb{background:#667eea;border-radius:3px}.code-example{background:#f5f5f5;padding:8px 12px;border-radius:8px;font-family:'Courier New',monospace;font-size:13px;margin:5px 0;color:#d63384}</style></head><body><div class="chat-container"><div class="chat-header"><h1>🤖 Asistente de Gestión de Pagos</h1><p>Gestiona tus facturas y pagos de forma inteligente</p></div><div id="connectionStatus" class="connection-status">⏳ Conectando al servidor...</div><div class="chat-messages" id="chatMessages"><div class="message bot"><div class="message-content">¡Hola! 👋 Soy tu asistente de pagos.

Puedo ayudarte a:
📝 Planificar facturas en cuotas
💰 Registrar pagos y abonos
🔍 Consultar información de facturas
💳 Ver tus deudas pendientes

Escribe "ayuda" para ver ejemplos de comandos.

¿En qué puedo ayudarte hoy?</div></div><div class="typing-indicator" id="typingIndicator"><span></span><span></span><span></span></div></div><div class="quick-actions"><button class="quick-btn" onclick="sendQuickMessage('Ver deudas pendientes')">💳 Ver deudas</button><button class="quick-btn" onclick="sendQuickMessage('Ver Sheets')">📊 Sheets & Calendar</button><button class="quick-btn" onclick="sendQuickMessage('ayuda')">❓ Ayuda</button></div><div class="chat-input-container"><div class="chat-input-wrapper"><input type="text" class="chat-input" id="userInput" placeholder="Escribe tu mensaje..." onkeypress="handleKeyPress(event)"><button class="send-btn" id="sendBtn" onclick="sendMessage()">➤</button></div></div></div><script>const API_URL=https://administrador-de-facturas-backend.onrender.com;const chatMessages=document.getElementById('chatMessages');const userInput=document.getElementById('userInput');const typingIndicator=document.getElementById('typingIndicator');const sendBtn=document.getElementById('sendBtn');const connectionStatus=document.getElementById('connectionStatus');async function sendMessageToBackend(message){try{const response=await fetch(`${API_URL}/api/chat`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:message})});if(!response.ok)throw new Error(`HTTP error! status: ${response.status}`);const data=await response.json();return{type:data.success?'success':'error',message:data.message,html:data.html||null};}catch(error){console.error('Error:',error);updateConnectionStatus('error');return{type:'error',message:`❌ Error de conexión: ${error.message}`};}}function updateConnectionStatus(status){if(status==='connected'){connectionStatus.className='connection-status connected';connectionStatus.textContent='✅ Conectado al servidor';}else if(status==='error'){connectionStatus.className='connection-status error';connectionStatus.textContent='❌ Error de conexión';}else{connectionStatus.className='connection-status';connectionStatus.textContent='⏳ Conectando...';}}async function checkServerStatus(){try{const response=await fetch(`${API_URL}/api/status`);const data=await response.json();if(data.status==='online'){updateConnectionStatus('connected');}}catch(error){updateConnectionStatus('error');}}function addMessage(message,isUser=false,html=null){const messageDiv=document.createElement('div');messageDiv.className=`message ${isUser?'user':'bot'}`;const contentDiv=document.createElement('div');contentDiv.className='message-content';if(html&&!isUser){contentDiv.innerHTML=html;}else{contentDiv.textContent=message;}messageDiv.appendChild(contentDiv);chatMessages.insertBefore(messageDiv,typingIndicator);chatMessages.scrollTop=chatMessages.scrollHeight;return messageDiv;}function showTypingIndicator(){typingIndicator.classList.add('active');chatMessages.scrollTop=chatMessages.scrollHeight;}function hideTypingIndicator(){typingIndicator.classList.remove('active');}async function sendMessage(){const message=userInput.value.trim();if(!message)return;addMessage(message,true);userInput.value='';sendBtn.disabled=true;userInput.disabled=true;userInput.placeholder='⏳ Procesando, espera...';showTypingIndicator();try{const response=await sendMessageToBackend(message);hideTypingIndicator();addMessage(response.message,false,response.html);if(response.type!=='error')updateConnectionStatus('connected');}catch(error){hideTypingIndicator();addMessage('❌ Error de conexión.',false);updateConnectionStatus('error');}finally{sendBtn.disabled=false;userInput.disabled=false;userInput.placeholder='Escribe tu mensaje...';userInput.focus();}}function sendQuickMessage(message){userInput.value=message;sendMessage();}function handleKeyPress(event){if(event.key==='Enter'&&!sendBtn.disabled)sendMessage();}window.addEventListener('load',()=>{userInput.focus();checkServerStatus();});</script></body></html>"""

def inicializar_servicios():
    global _sheets_service_cache, _calendar_service_cache
    
    if _sheets_service_cache is None:
        _sheets_service_cache = main.obtener_credenciales_google('Sheets')
    if _calendar_service_cache is None:
        _calendar_service_cache = main.obtener_credenciales_google('Calendar')
    
    return _sheets_service_cache, _calendar_service_cache


async def inicializar_runtime():
    """Crea un nuevo runtime completamente limpio"""
    new_runtime = SingleThreadedAgentRuntime()
    
    if main.SPREADSHEET_ID == 'TU_ID_DE_HOJA_DE_CALCULO':
        if os.path.exists('sheets_id.txt'):
            with open('sheets_id.txt', 'r') as f:
                main.SPREADSHEET_ID = f.read().strip()
    
    sheets_service, calendar_service = inicializar_servicios()
    
    await main.Organizador.register(new_runtime, "organizador", main.Organizador)
    await main.Planificador.register(new_runtime, "planificador", lambda: main.Planificador(sheets_service))
    await main.Notificador.register(new_runtime, "notificador", lambda: main.Notificador(calendar_service))
    await main.Registrador.register(new_runtime, "registrador", lambda: main.Registrador(sheets_service))
    await main.Consultor.register(new_runtime, "consultor", lambda: main.Consultor(sheets_service))
    
    new_runtime.start()
    return new_runtime

def formatear_respuesta_procesada(user_input: str, console_output: str):
    """Extrae información del console output y la formatea"""
    
    # Si no hay output, mensaje genérico
    if not console_output or len(console_output.strip()) < 10:
        return generar_respuesta_contextual(user_input)
    
    lines = console_output.split('\n')
    
    sheets_url = f"https://docs.google.com/spreadsheets/d/{main.SPREADSHEET_ID}"
    calendar_url = "https://calendar.google.com"
    links_html = f'<br><br>📊 <a href="{sheets_url}" target="_blank" class="sheets-link">📄 Abrir Google Sheets</a> <a href="{calendar_url}" target="_blank" class="sheets-link" style="background: #ea4335;">📅 Abrir Google Calendar</a>'
    
    # Detectar tipo de operación
    es_planificar = 'Planificación completada' in console_output or 'registrada en Google Sheets' in console_output
    es_pago = any(x in console_output for x in ['Pago procesado', 'cuota(s) afectada', 'PAGADA COMPLETAMENTE'])
    es_consulta = 'INFORMACIÓN DE FACTURA' in console_output or 'DEUDAS PENDIENTES' in console_output
    
    # PLANIFICAR
    if es_planificar:
        factura_match = re.search(r'Factura (\d+)', console_output)
        cuotas_match = re.search(r'registrada.*?\((\d+) cuota', console_output)
        
        factura_id = factura_match.group(1) if factura_match else 'N/A'
        num_cuotas = cuotas_match.group(1) if cuotas_match else '1'
        
        # Buscar montos y fechas
        cuotas_info = []
        for line in lines:
            cuota_match = re.search(r'Cuota (\d+):\s*\$?([\d,]+)\s*COP.*?Vence:\s*([\d-]+)', line)
            if cuota_match:
                cuotas_info.append({
                    'num': cuota_match.group(1), 
                    'monto': cuota_match.group(2), 
                    'fecha': cuota_match.group(3)
                })
        
        # Calcular monto total
        monto_total = sum(float(c['monto'].replace(',', '')) for c in cuotas_info) if cuotas_info else 0
        
        html = f"""<strong>✅ FACTURA PLANIFICADA EXITOSAMENTE</strong><br><br>
<strong>📋 Información General:</strong><br>
- Factura: <strong>{factura_id}</strong><br>
- Monto total: <strong>${monto_total:,.0f} COP</strong><br>
- Cuotas: <strong>{num_cuotas}</strong><br><br>"""
        
        if cuotas_info:
            html += "<strong>📅 Detalle de Cuotas:</strong><br><div style='font-family:monospace;font-size:12px;margin-top:10px'>"
            for cuota in cuotas_info:
                html += f"<div style='padding:5px 0;border-bottom:1px solid #eee'>💳 Cuota {cuota['num']}: <strong>${cuota['monto']} COP</strong><br>📅 Vencimiento: {cuota['fecha']}</div>"
            html += "</div>"
        
        html += f"<br><strong>✅ Registros actualizados:</strong><br>📊 Google Sheets actualizado con {num_cuotas} cuota(s)<br>📧 Recordatorios creados en Google Calendar" + links_html
        
        return f"✅ Factura {factura_id} planificada: {num_cuotas} cuotas", html
    
    # PAGAR
    elif es_pago:
        factura_match = re.search(r'Cuota\s+([\d-]+)', console_output)
        cuotas_match = re.search(r'(\d+)\s+cuota\(s\)\s+afectada', console_output)
        
        factura_id = factura_match.group(1) if factura_match else 'N/A'
        num_afectadas = cuotas_match.group(1) if cuotas_match else '1'
        
        html = f"""<strong>✅ PAGO REGISTRADO EXITOSAMENTE</strong><br><br>
- Referencia: <strong>{factura_id}</strong><br>
- Cuotas procesadas: <strong>{num_afectadas}</strong><br><br>
<strong>✅ Registros actualizados:</strong><br>
📊 Google Sheets actualizado<br>
📧 Calendar actualizado""" + links_html
        
        return f"✅ Pago registrado: {num_afectadas} cuota(s) procesada(s)", html
    
    # CONSULTA
    elif es_consulta:
        # Extraer información de consultas
        return "✅ Consulta realizada", f"<pre style='font-size:12px;background:#f5f5f5;padding:10px;border-radius:5px;overflow-x:auto'>{console_output}</pre>" + links_html
    
    # Si no detecta nada específico, usar respuesta contextual
    return generar_respuesta_contextual(user_input)

def generar_respuesta_contextual(user_input: str):
    user_lower = user_input.lower()
    
    sheets_url = f"https://docs.google.com/spreadsheets/d/{main.SPREADSHEET_ID}"
    calendar_url = "https://calendar.google.com"
    links_html = f'<br><br>📊 <a href="{sheets_url}" target="_blank" class="sheets-link">📄 Abrir Google Sheets</a> <a href="{calendar_url}" target="_blank" class="sheets-link" style="background: #ea4335;">📅 Abrir Google Calendar</a>'
    
    if any(word in user_lower for word in ['ayuda', 'help', 'comandos']):
        html = """<strong>💡 COMANDOS DISPONIBLES:</strong><br><br><strong>📝 PLANIFICAR:</strong><br><div class="code-example">"Factura 12345 por $500000 en 3 cuotas"</div><br><strong>💰 PAGAR:</strong><br><div class="code-example">"Pagué $200000 de la factura 12345"</div><br><strong>🔍 CONSULTAR:</strong><br><div class="code-example">"Consultar factura 12345"</div><div class="code-example">"Ver deudas pendientes"</div>""" + links_html
        return "💡 Comandos disponibles", html
    
    elif any(word in user_lower for word in ['sheets', 'calendar']):
        html = f"""<strong>📊 ACCESOS RÁPIDOS</strong><br><br><a href="{sheets_url}" target="_blank" class="sheets-link">📄 Google Sheets</a><br><a href="{calendar_url}" target="_blank" class="sheets-link" style="background: #ea4335; margin-left:0;">📅 Google Calendar</a>"""
        return "📊 Links de acceso", html
    
    else:
        return "✅ Mensaje procesado", f"✅ Mensaje procesado<br>Escribe 'ayuda' para ver comandos" + links_html

async def procesar_mensaje(user_input: str):
    """Procesa cada mensaje con un runtime limpio"""
    print(f"\n🔵 INICIO procesar_mensaje: '{user_input[:50]}...'")
    
    user_lower = user_input.lower()
    comandos_directos = ['ayuda', 'help', 'sheets', 'calendar']
    
    if any(cmd in user_lower for cmd in comandos_directos):
        print(f"🟢 Comando directo detectado, sin runtime")
        return generar_respuesta_contextual(user_input)
    
    print(f"🟡 Inicializando runtime...")
    
    try:
        # ✅ Crear runtime limpio para este mensaje
        local_runtime = await inicializar_runtime()
        print(f"✅ Runtime inicializado correctamente")
    except Exception as e:
        print(f"❌ Error al inicializar runtime: {e}")
        import traceback
        traceback.print_exc()
        return "Error al inicializar el sistema", "<strong>❌ Error del servidor</strong>"
    
    old_stdout = sys.stdout
    sys.stdout = buffer = io.StringIO()
    
    try:
        print(f"🟡 Creando mensaje...")
        data_mensaje = {"user_input": user_input}
        mensaje = main.PaymentMessage.model_validate(data_mensaje)
        
        print(f"🟡 Enviando mensaje al organizador...")
        await local_runtime.send_message(mensaje, AgentId("organizador", "default"))
        
        print(f"🟡 Esperando procesamiento...")
        await local_runtime.stop_when_idle()
        
        console_output = buffer.getvalue()
        print(f"✅ Procesamiento completo. Output: {len(console_output)} chars")
        
    except Exception as e:
        console_output = f"Error: {str(e)}"
        print(f"❌ Error en procesamiento: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sys.stdout = old_stdout
        print(f"🟡 Limpiando runtime...")
        # ✅ Limpiar el runtime después de usar
        try:
            await local_runtime.stop()
            print(f"✅ Runtime detenido")
        except Exception as e:
            print(f"⚠️ Error al detener runtime: {e}")
    
    print(f"🔵 FIN procesar_mensaje\n")
    return formatear_respuesta_procesada(user_input, console_output)


def get_user_token_path(user_id):
    """Obtiene la ruta del token para un usuario específico."""
    return TOKENS_DIR / f'token_{user_id}.pickle'

def get_credentials(user_id):
    """Obtiene las credenciales de un usuario específico."""
    token_path = get_user_token_path(user_id)
    
    creds = None
    if token_path.exists():
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)
    
    # Si las credenciales no son válidas, retornar None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Guardar credenciales actualizadas
                with open(token_path, 'wb') as token:
                    pickle.dump(creds, token)
                return creds
            except Exception as e:
                print(f"Error al refrescar token: {e}")
                return None
        return None
    
    return creds

def create_google_services(user_id):
    """Crea los servicios de Google para un usuario."""
    creds = get_credentials(user_id)
    
    if not creds:
        return None, None
    
    try:
        sheets_service = build('sheets', 'v4', credentials=creds)
        calendar_service = build('calendar', 'v3', credentials=creds)
        return sheets_service, calendar_service
    except Exception as e:
        print(f"Error al crear servicios: {e}")
        return None, None


@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    """Verifica si el usuario está autenticado."""
    user_id = session.get('user_id')
    
    if not user_id:
        return jsonify({
            'authenticated': False,
            'message': 'No hay sesión activa'
        })
    
    creds = get_credentials(user_id)
    
    if not creds:
        return jsonify({
            'authenticated': False,
            'message': 'Token expirado o inválido'
        })
    
    return jsonify({
        'authenticated': True,
        'user_id': user_id,
        'message': 'Sesión activa'
    })

@app.route('/api/auth/login', methods=['GET'])
def login():
    user_id = str(uuid.uuid4())
    state = str(uuid.uuid4())
    
    # ✅ Guardar user_id y state temporalmente en memoria (o Redis en producción)
    # Por ahora, los pasaremos en la URL de callback
    
    if os.environ.get('RENDER'):
        redirect_uri = 'https://administrador-de-facturas-backend.onrender.com/api/auth/callback'
    else:
        redirect_uri = 'http://localhost:5000/api/auth/callback'
    
    flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    authorization_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        state=f"{state}:{user_id}",  # ✅ Combinar state y user_id
        prompt='select_account'
    )
    
    return jsonify({
        'auth_url': authorization_url,
        'user_id': user_id
    })


@app.route('/api/auth/callback')
def oauth_callback():
    """Callback de OAuth - recibe el código de autorización."""
    
    # ✅ Extraer state y user_id de los query params
    state_param = request.args.get('state', '')
    
    try:
        state, user_id = state_param.split(':', 1)
    except ValueError:
        return jsonify({'error': 'Estado inválido'}), 400
    
    code = request.args.get('code')
    if not code:
        return jsonify({'error': 'Código no recibido'}), 400
    
    if os.environ.get('RENDER'):
        redirect_uri = 'https://administrador-de-facturas-backend.onrender.com/api/auth/callback'
    else:
        redirect_uri = 'http://localhost:5000/api/auth/callback'
    
    flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        
        # Guardar token del usuario
        token_path = get_user_token_path(user_id)
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)
        
        # ✅ Crear sesión DESPUÉS de autenticar
        session['user_id'] = user_id
        
        # Redirigir al frontend
        frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')   
        return redirect(f'{frontend_url}?auth=success&user_id={user_id}')
    
    except Exception as e:
        print(f"Error en OAuth: {e}")
        frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')
        return redirect(f'{frontend_url}?auth=error&message={str(e)}')


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """Cierra la sesión del usuario."""
    user_id = session.get('user_id')
    
    if user_id:
        # Opcional: Eliminar el token del usuario
        token_path = get_user_token_path(user_id)
        if token_path.exists():
            token_path.unlink()
    
    session.clear()
    
    return jsonify({
        'success': True,
        'message': 'Sesión cerrada'
    })

@app.route('/api/chat', methods=['POST'])
def chat():
    """Procesa mensajes del chat (requiere autenticación)."""
    user_id = session.get('user_id')
    
    if not user_id:
        return jsonify({
            'success': False,
            'message': '❌ No estás autenticado. Por favor inicia sesión.'
        }), 401
    
    # Verificar que el usuario tenga credenciales válidas
    sheets_service, calendar_service = create_google_services(user_id)
    
    if not sheets_service or not calendar_service:
        return jsonify({
            'success': False,
            'message': '❌ Tu sesión ha expirado. Por favor vuelve a iniciar sesión.'
        }), 401
    
    try:
        data = request.json
        user_message = data.get('message', '').strip()
        
        if not user_message:
            return jsonify({'success': False, 'message': '❌ Mensaje vacío'}), 400
        
        # TODO: Aquí va tu lógica de procesamiento de mensajes
        # Por ahora, respuesta de prueba
        result_text = f"✅ Mensaje recibido: {user_message}"
        result_html = None
        
        return jsonify({
            'success': True,
            'message': result_text,
            'html': result_html
        })
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'❌ Error: {str(e)}'
        }), 500


@app.route('/api/status', methods=['GET'])
def status():
    """Estado del servidor."""
    user_id = session.get('user_id')
    
    return jsonify({
        'status': 'online',
        'authenticated': user_id is not None,
        'user_id': user_id
    })

@app.route('/')
def index():
    return jsonify({
        'message': 'Backend de Asistente de Pagos',
        'version': '2.0',
        'endpoints': {
            'auth_status': '/api/auth/status',
            'login': '/api/auth/login',
            'logout': '/api/auth/logout',
            'chat': '/api/chat',
            'status': '/api/status'
        }
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, port=port, host='0.0.0.0')