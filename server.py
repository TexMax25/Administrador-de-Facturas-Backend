#server.py
from flask import Flask, request, jsonify, session, make_response, redirect
from flask_cors import CORS
import asyncio
import os

if os.environ.get('FLASK_ENV') == 'development' or os.environ.get('INSECURE_OAUTH') == '1' or not os.environ.get('RENDER'):
    os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')
from datetime import datetime
from threading import Lock
import sys
import io
import pickle
import re
import secrets
from urllib.parse import quote_plus

from pathlib import Path
import uuid

sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__
os.environ['PYTHONUNBUFFERED'] = '1'

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
    SESSION_COOKIE_SECURE=True,  
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='None',  
    SESSION_COOKIE_DOMAIN='.onrender.com',
    PERMANENT_SESSION_LIFETIME=3600  
)

CORS(app, origins=[
    "https://texmax25.github.io",
    "http://localhost:5000"
], supports_credentials=True)

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/userinfo.email', 
    'openid' 
]

# Directorio de tokens por usuario
TOKENS_DIR = Path('user_tokens')
TOKENS_DIR.mkdir(parents=True, exist_ok=True)

# Variables globales
runtime_lock = Lock()
user_sessions = {}

# Cache para servicios (solo desarrollo local)
_sheets_service_cache = None
_calendar_service_cache = None


# ============================================================================
# FUNCIONES DE GESTIÓN DE CREDENCIALES POR USUARIO
# ============================================================================

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
    
    # Si las credenciales no son válidas, intentar refrescar
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
    """Crea los servicios de Google para un usuario específico."""
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


def inicializar_servicios():
    """
    Solo para desarrollo local - NO usar en producción.
    En producción, usar create_google_services(user_id)
    """
    global _sheets_service_cache, _calendar_service_cache
    
    # Solo en desarrollo local
    if not os.environ.get('RENDER'):
        if _sheets_service_cache is None:
            _sheets_service_cache = main.obtener_credenciales_google('Sheets')
        if _calendar_service_cache is None:
            _calendar_service_cache = main.obtener_credenciales_google('Calendar')
        return _sheets_service_cache, _calendar_service_cache
    
    # En producción, retornar None
    return None, None


def get_user_sheets_id_path(user_id):
    """Obtiene la ruta del archivo donde se guarda el Sheets ID del usuario."""
    return TOKENS_DIR / f'sheets_{user_id}.txt'

def get_user_sheets_id(user_id):
    """Obtiene el Sheets ID de un usuario específico."""
    sheets_id_path = get_user_sheets_id_path(user_id)
    if sheets_id_path.exists():
        with open(sheets_id_path, 'r') as f:
            return f.read().strip()
    return None

def save_user_sheets_id(user_id, sheets_id):
    """Guarda el Sheets ID de un usuario."""
    sheets_id_path = get_user_sheets_id_path(user_id)
    with open(sheets_id_path, 'w') as f:
        f.write(sheets_id)
    print(f"✅ Sheets ID guardado para usuario {user_id[:8]}: {sheets_id}")


# ============================================================================
# FUNCIONES DE RUNTIME Y PROCESAMIENTO
# ============================================================================

async def inicializar_runtime(user_id):
    """Crea un nuevo runtime usando las credenciales y Sheets del usuario específico."""
    new_runtime = SingleThreadedAgentRuntime()
    
    # 🔥 CRÍTICO: Obtener o crear el Sheets ID del usuario
    user_sheets_id = get_user_sheets_id(user_id)
    
    if not user_sheets_id:
        # El usuario no tiene Sheets, crear uno nuevo
        print(f"📋 Usuario {user_id[:8]} no tiene Sheets, creando uno nuevo...")
        sheets_service_temp, _ = create_google_services(user_id)
        
        if sheets_service_temp:
            new_sheets_id, sheets_url = main.crear_hoja_calculo(
                sheets_service_temp,
                f"Gestor de Pagos - Usuario {user_id[:8]}"
            )
            
            if new_sheets_id:
                user_sheets_id = new_sheets_id
                save_user_sheets_id(user_id, new_sheets_id)
                print(f"✅ Sheets creado: {sheets_url}")
            else:
                raise ValueError("No se pudo crear el Google Sheets")
        else:
            raise ValueError("No se pudieron obtener credenciales para crear Sheets")
    else:
        print(f"✅ Usando Sheets existente del usuario: {user_sheets_id}")
    
    # 🔥 ASIGNAR el Sheets ID del usuario a main.py
    main.SPREADSHEET_ID = user_sheets_id
    
    # Crear servicios con credenciales del usuario
    sheets_service, calendar_service = create_google_services(user_id)
    
    if not sheets_service or not calendar_service:
        raise ValueError("No se pudieron crear los servicios de Google para este usuario")
    
    # Registrar agentes con los servicios del usuario
    await main.Organizador.register(new_runtime, "organizador", main.Organizador)
    await main.Planificador.register(new_runtime, "planificador", lambda: main.Planificador(sheets_service))
    await main.Notificador.register(new_runtime, "notificador", lambda: main.Notificador(calendar_service))
    await main.Registrador.register(new_runtime, "registrador", lambda: main.Registrador(sheets_service))
    await main.Consultor.register(new_runtime, "consultor", lambda: main.Consultor(sheets_service))
    
    new_runtime.start()
    return new_runtime


async def procesar_mensaje(user_input: str, user_id: str):
    """Procesa cada mensaje con un runtime limpio usando credenciales del usuario."""
    print(f"\n{'='*70}")
    print(f"🔵 INICIO procesar_mensaje")
    print(f"📝 Input: '{user_input}'")
    print(f"👤 Usuario: {user_id[:8]}")
    print(f"{'='*70}")
    
    user_lower = user_input.lower()
    comandos_directos = ['ayuda', 'help', 'sheets', 'calendar']
    
    if any(cmd in user_lower for cmd in comandos_directos):
        print(f"🟢 Comando directo detectado, sin runtime")
        return generar_respuesta_contextual(user_input)
    
    print(f"🟡 Inicializando runtime para usuario {user_id[:8]}...")
    
    try:
        # ✅ Pasar user_id al inicializar runtime
        local_runtime = await inicializar_runtime(user_id)
        print(f"✅ Runtime inicializado correctamente")
    except ValueError as e:
        print(f"❌ Error de credenciales: {e}")
        return ("❌ Sesión expirada", 
                "<strong>❌ Tu sesión ha expirado</strong><br>Por favor, cierra sesión y vuelve a autenticarte.")
    except Exception as e:
        print(f"❌ Error al inicializar runtime: {e}")
        import traceback
        traceback.print_exc()
        return ("❌ Error del servidor", 
                "<strong>❌ Error del servidor</strong><br>Por favor, intenta de nuevo.")
    
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
        
        # 🔥 NUEVO: Mostrar parte del output para debugging
        if console_output:
            preview = console_output[:500]
            print(f"📄 Output preview:\n{preview}")
            if len(console_output) > 500:
                print(f"... (truncado, total: {len(console_output)} chars)")
        else:
            print(f"⚠️ ADVERTENCIA: Output vacío del runtime")
        
    except Exception as e:
        console_output = f"Error: {str(e)}"
        print(f"❌ Error en procesamiento: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sys.stdout = old_stdout
        print(f"🟡 Limpiando runtime...")
        try:
            await local_runtime.stop()
            print(f"✅ Runtime detenido")
        except Exception as e:
            print(f"⚠️ Error al detener runtime: {e}")
    
    print(f"🔵 FIN procesar_mensaje")
    print(f"{'='*70}\n")
    return formatear_respuesta_procesada(user_input, console_output, user_id)


# ============================================================================
# FUNCIONES DE FORMATEO DE RESPUESTAS
# ============================================================================

def formatear_respuesta_procesada(user_input: str, console_output: str, user_id: str):
    """Extrae información del console output y la formatea usando el Sheets del usuario."""
    
    # 🔥 Obtener el Sheets ID específico del usuario
    user_sheets_id = get_user_sheets_id(user_id)
    if not user_sheets_id:
        user_sheets_id = main.SPREADSHEET_ID  # Fallback al ID global si no existe
    
    # 🔥 MEJORADO: Detectar output vacío o muy corto
    if not console_output or len(console_output.strip()) < 10:
        print(f"⚠️ ADVERTENCIA: Output insuficiente ({len(console_output) if console_output else 0} chars)")
        print(f"⚠️ Esto puede indicar que los agentes no procesaron el mensaje")
        
        # Si parece un comando de planificación/pago pero no hay output, advertir
        user_lower = user_input.lower()
        if any(word in user_lower for word in ['factura', 'pagar', 'pagué', 'abono', 'cuota']):
            sheets_url = f"https://docs.google.com/spreadsheets/d/{user_sheets_id}"
            return ("⚠️ Error en procesamiento", 
                    "<strong>⚠️ El sistema no pudo procesar tu solicitud</strong><br><br>"
                    "Posibles causas:<br>"
                    "• El servicio de IA está sobrecargado<br>"
                    "• Error en la comunicación con Google Sheets<br><br>"
                    f"Por favor, intenta de nuevo en unos segundos.<br><br>"
                    f'📊 <a href="{sheets_url}" target="_blank" class="sheets-link">Ver tu Google Sheets</a>')
        
        return generar_respuesta_contextual(user_input, user_sheets_id)
    
    lines = console_output.split('\n')
    
    sheets_url = f"https://docs.google.com/spreadsheets/d/{user_sheets_id}"
    calendar_url = "https://calendar.google.com"
    links_html = f'<br><br>📊 <a href="{sheets_url}" target="_blank" class="sheets-link">📄 Abrir tu Google Sheets</a> <a href="{calendar_url}" target="_blank" class="sheets-link" style="background: #ea4335;">📅 Abrir Google Calendar</a>'
    
    # Detectar tipo de operación
    es_planificar = 'Planificación completada' in console_output or 'registrada en Google Sheets' in console_output
    es_pago = any(x in console_output for x in ['Pago procesado', 'cuota(s) afectada', 'PAGADA COMPLETAMENTE'])
    es_consulta = 'INFORMACIÓN DE FACTURA' in console_output or 'DEUDAS PENDIENTES' in console_output
    
    # 🔥 Detectar errores de OpenRouter
    if 'ERROR' in console_output and any(x in console_output for x in ['OpenRouter', 'API Key', 'sobrecargados']):
        return ("⚠️ Servicio temporalmente no disponible",
                "<strong>⚠️ El servicio de IA está temporalmente sobrecargado</strong><br><br>"
                "Por favor, espera 1-2 minutos y vuelve a intentar.<br><br>"
                "💡 O intenta comandos directos: 'ayuda', 'ver deudas'" + links_html)
    
    # PLANIFICAR
    if es_planificar:
        factura_match = re.search(r'Factura (\d+)', console_output)
        cuotas_match = re.search(r'registrada.*?\((\d+) cuota', console_output)
        
        factura_id = factura_match.group(1) if factura_match else 'N/A'
        num_cuotas = cuotas_match.group(1) if cuotas_match else '1'
        
        cuotas_info = []
        for line in lines:
            cuota_match = re.search(r'Cuota (\d+):\s*\$?([\d,]+)\s*COP.*?Vence:\s*([\d-]+)', line)
            if cuota_match:
                cuotas_info.append({
                    'num': cuota_match.group(1), 
                    'monto': cuota_match.group(2), 
                    'fecha': cuota_match.group(3)
                })
        
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
        return "✅ Consulta realizada", f"<pre style='font-size:12px;background:#f5f5f5;padding:10px;border-radius:5px;overflow-x:auto'>{console_output}</pre>" + links_html
    
    # 🔥 Si hay output pero no se detectó ninguna operación
    print(f"⚠️ ADVERTENCIA: Output presente pero no se detectó operación específica")
    return ("✅ Mensaje procesado", 
            f"✅ Mensaje procesado<br><br>"
            f"<details style='margin-top:10px'><summary style='cursor:pointer;color:#667eea'>Ver log del sistema</summary>"
            f"<pre style='font-size:11px;background:#f8f9fa;padding:10px;border-radius:5px;max-height:300px;overflow:auto'>{console_output[:1000]}</pre>"
            f"</details>" + links_html)


def generar_respuesta_contextual(user_input: str, user_sheets_id: str = None):
    """Genera respuestas para comandos directos sin procesamiento."""
    user_lower = user_input.lower()
    
    # Usar el Sheets ID del usuario si está disponible
    if not user_sheets_id:
        user_sheets_id = main.SPREADSHEET_ID
    
    sheets_url = f"https://docs.google.com/spreadsheets/d/{user_sheets_id}"
    calendar_url = "https://calendar.google.com"
    links_html = f'<br><br>📊 <a href="{sheets_url}" target="_blank" class="sheets-link">📄 Abrir tu Google Sheets</a> <a href="{calendar_url}" target="_blank" class="sheets-link" style="background: #ea4335;">📅 Abrir Google Calendar</a>'
    
    if any(word in user_lower for word in ['ayuda', 'help', 'comandos']):
        html = """<strong>💡 COMANDOS DISPONIBLES:</strong><br><br>
<strong>📝 PLANIFICAR:</strong><br>
<div class="code-example">"Factura 12345 por $500000 en 3 cuotas"</div><br>
<strong>💰 PAGAR:</strong><br>
<div class="code-example">"Pagué $200000 de la factura 12345"</div><br>
<strong>🔍 CONSULTAR:</strong><br>
<div class="code-example">"Consultar factura 12345"</div>
<div class="code-example">"Ver deudas pendientes"</div>""" + links_html
        return "💡 Comandos disponibles", html
    
    elif any(word in user_lower for word in ['sheets', 'calendar', 'link']):
        html = f"""<strong>📊 TUS ACCESOS RÁPIDOS</strong><br><br>
<a href="{sheets_url}" target="_blank" class="sheets-link">📄 Tu Google Sheets</a><br>
<a href="{calendar_url}" target="_blank" class="sheets-link" style="background: #ea4335; margin-left:0;">📅 Google Calendar</a><br><br>
<p style="font-size:12px;color:#666;margin-top:15px;">
💡 Cada usuario tiene su propio Google Sheets personal. Tus datos están seguros y separados.
</p>"""
        return "📊 Links de acceso", html
    
    else:
        return "✅ Mensaje procesado", f"✅ Mensaje procesado<br>Escribe 'ayuda' para ver comandos" + links_html


# ============================================================================
# RUTAS DE AUTENTICACIÓN
# ============================================================================

@app.route('/api/auth/login', methods=['GET'])
def login():
    """Inicia el flujo de OAuth2."""
    # 🔥 NO generar user_id aquí, solo el state
    state = str(uuid.uuid4())
    
    # Guardar temporalmente SOLO el state (sin user_id)
    user_sessions[state] = {
        'timestamp': datetime.now()
    }
    
    # Construir redirect_uri según el entorno
    if os.environ.get('RENDER'):
        redirect_uri = 'https://administrador-de-facturas-backend.onrender.com/api/auth/callback'
    else:
        port = os.environ.get('PORT', '5000')
        redirect_uri = f'http://localhost:{port}/api/auth/callback'
    
    flow = InstalledAppFlow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    
    authorization_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        state=state,
        prompt='select_account'
    )
    
    print(f"🔵 Login iniciado - State: {state}")
    
    return jsonify({
        'auth_url': authorization_url
    })


@app.route('/api/auth/callback')
def oauth_callback():
    """Callback de OAuth - recibe el código de autorización."""
    
    state = request.args.get('state', '')
    code = request.args.get('code')
    error = request.args.get('error')
    
    print(f"🔵 === CALLBACK RECIBIDO ===")
    print(f"State: {state}")
    print(f"Code: {code[:30] if code else 'None'}...")
    print(f"Error: {error}")
    
    # Si hay error de Google
    if error:
        print(f"❌ Error de Google OAuth: {error}")
        frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')
        return redirect(f'{frontend_url}?auth=error&message={error}')
    
    # Verificar que el state existe
    session_data = user_sessions.get(state)
    
    if not session_data:
        print("❌ Error: State no encontrado en sesiones")
        frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')
        return f"""
        <html>
        <body style="font-family: Arial; padding: 40px; background: #f5f5f5;">
            <div style="background: white; padding: 30px; border-radius: 10px; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #dc3545;">❌ Error de Autenticación</h2>
                <p><strong>Problema:</strong> Sesión expirada o inválida</p>
                <p>Por favor, intenta iniciar sesión nuevamente.</p>
                <a href="{frontend_url}" 
                   style="display: inline-block; margin-top: 20px; padding: 10px 20px; 
                          background: #667eea; color: white; text-decoration: none; 
                          border-radius: 5px;">
                    🔙 Volver a la aplicación
                </a>
            </div>
        </body>
        </html>
        """, 400
    
    if not code:
        print("❌ Error: No se recibió código de autorización")
        frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')
        return f"""
        <html>
        <body style="font-family: Arial; padding: 40px; background: #f5f5f5;">
            <div style="background: white; padding: 30px; border-radius: 10px; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #dc3545;">❌ Error de Autenticación</h2>
                <p><strong>Problema:</strong> No se recibió código de autorización</p>
                <a href="{frontend_url}" 
                   style="display: inline-block; margin-top: 20px; padding: 10px 20px; 
                          background: #667eea; color: white; text-decoration: none; 
                          border-radius: 5px;">
                    🔙 Volver a la aplicación
                </a>
            </div>
        </body>
        </html>
        """, 400
    
    # Detectar entorno
    is_local = not os.environ.get('RENDER')
    redirect_uri = 'http://localhost:5000/api/auth/callback' if is_local else 'https://administrador-de-facturas-backend.onrender.com/api/auth/callback'
    
    print(f"🔵 Usando redirect_uri: {redirect_uri}")
    
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            'credentials.json',
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
        
        print(f"🔵 Intercambiando código por token...")
        flow.fetch_token(code=code)
        creds = flow.credentials
        
        print(f"✅ Token obtenido exitosamente")
        
        # 🔥 OBTENER EMAIL DEL USUARIO (3 métodos)
        user_email = None
        user_id = None
        
        # MÉTODO 1: Intentar desde id_token (más rápido)
        try:
            if hasattr(creds, 'id_token') and creds.id_token:
                import jwt
                decoded_token = jwt.decode(creds.id_token, options={"verify_signature": False})
                user_email = decoded_token.get('email')
                
                if user_email:
                    print(f"✅ Email obtenido del id_token: {user_email}")
                else:
                    print(f"⚠️ id_token no contiene email")
        except Exception as e:
            print(f"⚠️ No se pudo decodificar id_token: {e}")
        
        # MÉTODO 2: Usar la API userinfo
        if not user_email:
            try:
                print(f"🔄 Intentando obtener email desde userinfo API...")
                oauth2_service = build('oauth2', 'v2', credentials=creds)
                user_info = oauth2_service.userinfo().get().execute()
                user_email = user_info.get('email')
                
                if user_email:
                    print(f"✅ Email obtenido de userinfo API: {user_email}")
                else:
                    print(f"⚠️ userinfo API no devolvió email")
            except Exception as e:
                print(f"⚠️ Error en userinfo API: {e}")
        
        # MÉTODO 3 (FALLBACK): Usar Google SUB como identificador único
        if not user_email:
            try:
                print(f"🔄 Intentando usar Google SUB como identificador...")
                if hasattr(creds, 'id_token') and creds.id_token:
                    import jwt
                    decoded_token = jwt.decode(creds.id_token, options={"verify_signature": False})
                    google_sub = decoded_token.get('sub')
                    
                    if google_sub:
                        # Usar el SUB de Google como identificador único
                        import hashlib
                        user_id = hashlib.sha256(google_sub.encode()).hexdigest()[:16]
                        user_email = f"user_{user_id}@google.placeholder"
                        print(f"✅ Usando Google SUB: {google_sub}")
                        print(f"✅ User ID generado: {user_id}")
                    else:
                        raise ValueError("No se encontró SUB en id_token")
                else:
                    raise ValueError("No hay id_token disponible")
            except Exception as e:
                print(f"❌ Error crítico al obtener identificador: {e}")
                import traceback
                traceback.print_exc()
                
                frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')
                return f"""
                <html>
                <body style="font-family: Arial; padding: 40px; background: #f5f5f5;">
                    <div style="background: white; padding: 30px; border-radius: 10px; max-width: 600px; margin: 0 auto;">
                        <h2 style="color: #dc3545;">❌ Error de Autenticación</h2>
                        <p><strong>Problema:</strong> No se pudo obtener un identificador único de tu cuenta de Google</p>
                        <p><strong>Error:</strong> {str(e)}</p>
                        <hr style="margin: 20px 0;">
                        <p><strong>Soluciones:</strong></p>
                        <ol style="text-align: left; margin: 15px 0;">
                            <li>Cierra todas tus sesiones de Google</li>
                            <li>Vuelve a intentar iniciar sesión</li>
                            <li>Asegúrate de aceptar TODOS los permisos</li>
                        </ol>
                        <a href="{frontend_url}" 
                           style="display: inline-block; margin-top: 20px; padding: 10px 20px; 
                                  background: #667eea; color: white; text-decoration: none; 
                                  border-radius: 5px;">
                            🔙 Volver a la aplicación
                        </a>
                    </div>
                </body>
                </html>
                """, 500
        
        # Si tenemos email pero no user_id, generarlo ahora
        if user_email and not user_id:
            import hashlib
            user_id = hashlib.sha256(user_email.encode()).hexdigest()[:16]
            print(f"✅ User ID permanente generado: {user_id}")
        
        # Guardar token con el user_id permanente
        token_path = get_user_token_path(user_id)
        TOKENS_DIR.mkdir(exist_ok=True)
        
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)
        
        print(f"✅ Token guardado en: {token_path}")
        
        # Limpiar el state temporal
        del user_sessions[state]
        print(f"✅ State limpiado")
        
        # Generar token de sesión
        session_token = secrets.token_urlsafe(32)
        user_sessions[session_token] = {
            'user_id': user_id,
            'timestamp': datetime.now(),
            'email': user_email
        }
        
        print(f"✅ Sesión creada")
        print(f"   Email: {user_email}")
        print(f"   User ID: {user_id}")
        
        # Redirigir al frontend
        frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')
        redirect_url = f'{frontend_url}?auth=success&token={quote_plus(session_token)}'
        print(f"✅ Redirigiendo a: {redirect_url}")
        return redirect(redirect_url)
    
    except Exception as e:
        print(f"❌ Error en OAuth: {e}")
        import traceback
        error_trace = traceback.format_exc()
        print(error_trace)
        
        frontend_url = os.environ.get('FRONTEND_URL', 'https://texmax25.github.io/Administrador-de-Facturas')
        return f"""
        <html>
        <body style="font-family: Arial; padding: 40px; background: #f5f5f5;">
            <div style="background: white; padding: 30px; border-radius: 10px; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #dc3545;">❌ Error al Obtener Credenciales</h2>
                <p><strong>Error:</strong> {str(e)}</p>
                <details style="margin-top: 20px;">
                    <summary style="cursor: pointer; color: #667eea;">Ver detalles técnicos</summary>
                    <pre style="background: #f8f9fa; padding: 15px; border-radius: 5px; overflow-x: auto; font-size: 12px;">{error_trace}</pre>
                </details>
                <a href="{frontend_url}" 
                   style="display: inline-block; margin-top: 20px; padding: 10px 20px; 
                          background: #667eea; color: white; text-decoration: none; 
                          border-radius: 5px;">
                    🔙 Volver a la aplicación
                </a>
            </div>
        </body>
        </html>
        """, 500


@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    """Verifica si el usuario está autenticado usando token."""
    auth_header = request.headers.get('Authorization', '')
    
    if not auth_header.startswith('Bearer '):
        return jsonify({
            'authenticated': False,
            'message': 'No hay token'
        })
    
    token = auth_header.replace('Bearer ', '')
    session_data = user_sessions.get(token)
    
    if not session_data:
        return jsonify({
            'authenticated': False,
            'message': 'Token inválido o expirado'
        })
    
    user_id = session_data['user_id']
    creds = get_credentials(user_id)
    
    if not creds:
        return jsonify({
            'authenticated': False,
            'message': 'Credenciales expiradas'
        })
    
    return jsonify({
        'authenticated': True,
        'user_id': user_id,
        'message': 'Sesión activa'
    })


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    """Cierra la sesión del usuario."""
    auth_header = request.headers.get('Authorization', '')
    
    if auth_header.startswith('Bearer '):
        token = auth_header.replace('Bearer ', '')
        session_data = user_sessions.get(token)
        
        if session_data:
            user_id = session_data['user_id']
            # Opcional: Eliminar el token del usuario
            token_path = get_user_token_path(user_id)
            if token_path.exists():
                token_path.unlink()
            
            # Eliminar sesión
            del user_sessions[token]
    
    return jsonify({
        'success': True,
        'message': 'Sesión cerrada'
    })


# ============================================================================
# RUTAS DE LA APLICACIÓN
# ============================================================================

@app.route('/api/chat', methods=['POST'])
def chat():
    """Procesa mensajes del chat (requiere autenticación)."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({
            'success': False, 
            'message': '❌ No estás autenticado. Por favor inicia sesión.'
        }), 401

    token = auth_header.replace('Bearer ', '')
    session_data = user_sessions.get(token)
    
    if not session_data:
        return jsonify({
            'success': False, 
            'message': '❌ Tu sesión ha expirado. Por favor vuelve a iniciar sesión.'
        }), 401

    user_id = session_data['user_id']
    print(f"\n{'='*60}")
    print(f"📥 Mensaje de usuario: {user_id[:8]}")
    print(f"{'='*60}")

    # Verificar servicios Google
    sheets_service, calendar_service = create_google_services(user_id)
    if not sheets_service or not calendar_service:
        return jsonify({
            'success': False, 
            'message': '❌ No se encontraron credenciales válidas. Por favor, cierra sesión y vuelve a autenticarte.'
        }), 401

    try:
        data = request.json or {}
        user_message = (data.get('message') or '').strip()
        if not user_message:
            return jsonify({'success': False, 'message': '❌ Mensaje vacío'}), 400

        # 🔥 Ejecutar el procesamiento pasando user_id
        result = asyncio.run(procesar_mensaje(user_message, user_id))
        
        if isinstance(result, tuple) and len(result) >= 2:
            result_text, result_html = result[0], result[1]
        else:
            result_text, result_html = str(result), None

        return jsonify({
            'success': True,
            'message': result_text,
            'html': result_html
        })

    except Exception as e:
        print(f"❌ Error en chat(): {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False, 
            'message': f'❌ Error: {str(e)}'
        }), 500

@app.route('/api/user/sheets-url', methods=['GET'])
def get_user_sheets_url():
    """Devuelve el link del Google Sheets del usuario."""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'message': 'No autenticado'}), 401

    token = auth_header.replace('Bearer ', '')
    session_data = user_sessions.get(token)
    
    if not session_data:
        return jsonify({'success': False, 'message': 'Sesión expirada'}), 401

    user_id = session_data['user_id']
    sheets_id = get_user_sheets_id(user_id)
    
    if sheets_id:
        sheets_url = f"https://docs.google.com/spreadsheets/d/{sheets_id}"
        return jsonify({
            'success': True,
            'sheets_id': sheets_id,
            'sheets_url': sheets_url
        })
    else:
        return jsonify({
            'success': False,
            'message': 'El usuario aún no tiene un Sheets creado'
        })


@app.route('/api/status', methods=['GET'])
def status():
    """Estado del servidor."""
    return jsonify({
        'status': 'online',
        'message': 'Backend de Asistente de Pagos funcionando'
    })


@app.route('/')
def index():
    """Información de la API."""
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


# ============================================================================
# INICIO DE LA APLICACIÓN
# ============================================================================

if __name__ == '__main__':
    # 🔥 NUEVO: Configurar logging
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    
    port = int(os.environ.get('PORT', 5000))
    print(f"\n{'='*70}")
    print(f"🚀 SERVIDOR INICIANDO EN PUERTO {port}")
    print(f"{'='*70}\n")
    
    app.run(debug=False, port=port, host='0.0.0.0')