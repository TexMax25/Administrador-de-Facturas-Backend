#Main.py
import asyncio
import httpx
import json
import os
import sys
import pickle
from datetime import datetime, date, timedelta
from pydantic import BaseModel, Field
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from autogen_core import DefaultTopicId, MessageContext, RoutedAgent, default_subscription, message_handler
from autogen_core import AgentId, SingleThreadedAgentRuntime

# --- 1. CONSTANTES DE API y CONFIGURACIÓN ---

OPENROUTER_API_KEY = "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" 
YOUR_SITE_URL = "https://mi-organizador-pagos.com" 
YOUR_SITE_NAME = "Organizador Pagos AutoGen" 
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_FALLBACK = [
    "google/gemini-2.0-flash-exp:free",       # Gemini 2.0 (experimental pero potente)
    "tngtech/deepseek-r1t2-chimera:free",     # DeepSeek (muy bueno para razonamiento)
    "z-ai/glm-4.5-air:free",                  # GLM-4 (bueno en español)
    "nvidia/nemotron-nano-12b-v2-vl:free",    # Nvidia Nemotron (rápido)
    "openai/gpt-oss-120b",                    # GPT OSS (open source)
]

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/spreadsheets'
]

CALENDAR_ID = 'primary'
SPREADSHEET_ID = 'TU_ID_DE_HOJA_DE_CALCULO' 
SHEETS_RANGE = 'Deuda Pendiente!A:H'

# --- 2. DATA CLASS PARA MENSAJES ---

class PaymentMessage(BaseModel):
    """Mensaje que se pasa entre agentes, conteniendo la información de la solicitud."""
    
    user_input: str  
    intent: Optional[str] = None
    status: str = "INITIAL"
    
    data: dict = Field(default_factory=lambda: {
        "monto_total": 0.0,
        "monto_pendiente": 0.0,
        "fracciones": 1, 
        "monto_abono": 0.0, 
        "fecha_actual": datetime.now().strftime("%Y-%m-%d"), 
        "fechas_pago": [],
        "numero_factura": "N/A" 
    })


# --- 3. FUNCIONES DE SERVICIO Y AUTENTICACIÓN ---

def obtener_credenciales_google(api_name):
    """
    Maneja el flujo de autenticación de OAuth2 para el usuario.
    """
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists('credentials.json'):
                print("¡ERROR FATAL! No se encontró 'credentials.json'. Colócalo en la carpeta del proyecto.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
            
    try:
        if api_name == 'Calendar':
            return build('calendar', 'v3', credentials=creds)
        elif api_name == 'Sheets':
            return build('sheets', 'v4', credentials=creds)
    except Exception as e:
        print(f"ERROR: Fallo al construir el servicio {api_name}: {e}")
        return None
    return None

def crear_hoja_calculo(service, title):
    """Crea una hoja de cálculo real en Google Sheets con las pestañas necesarias."""
    spreadsheet = {
        'properties': {'title': title},
        'sheets': [
            {'properties': {'title': 'Deuda Pendiente'}},
            {'properties': {'title': 'Historial de Pagos'}}
        ]
    }
    try:
        spreadsheet_response = service.spreadsheets().create(
            body=spreadsheet, fields='spreadsheetId,spreadsheetUrl'
        ).execute()
        spreadsheet_id = spreadsheet_response['spreadsheetId']
        spreadsheet_url = spreadsheet_response['spreadsheetUrl']

        encabezados_pendiente = [
            ["Fecha Registro", "Cuota ID", "Monto Total Factura", "Monto Pendiente Actual", "Monto Cuota Original",
             "Fecha Vencimiento", "Tipo de Pago", "Estado"]
        ]
        
        encabezados_historial = [
            ["Fecha y Hora Pago", "Cuota ID", "Tipo Transacción", "Monto Pagado", "Saldo Restante", "Observaciones"]
        ]
        
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range='Deuda Pendiente!A1',
            valueInputOption='USER_ENTERED',
            body={'values': encabezados_pendiente}
        ).execute()
        
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range='Historial de Pagos!A1',
            valueInputOption='USER_ENTERED',
            body={'values': encabezados_historial}
        ).execute()

        print(f"✅ Google Sheets creado: {spreadsheet_url}")
        return spreadsheet_id, spreadsheet_url
    except HttpError as e:
         print(f"❌ Error de API al crear hoja de cálculo: {e.content}")
         return None, None

def _normalize_sheet_date(value):
    """
    Convierte valores de fecha, incluyendo el formato serial de Sheets/Excel.
    """
    if value is None: return None
    if isinstance(value, (datetime, date)): return value.strftime('%Y-%m-%d')
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s)
        return dt.date().strftime('%Y-%m-%d')
    except Exception: pass
    try:
        s_num = s.replace(',', '.')
        serial = float(s_num)
        origin = date(1899, 12, 30)
        fecha = origin + timedelta(days=int(serial))
        return fecha.strftime('%Y-%m-%d')
    except Exception: pass
    for fmt in ('%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.date().strftime('%Y-%m-%d')
        except Exception: pass
    return None

# En main.py, REEMPLAZA completamente la función call_openrouter() con esta:

async def call_openrouter(system_prompt: str, user_prompt: str) -> str:
    """
    Llama a OpenRouter con reintentos agresivos y delays progresivos.
    Persiste hasta 3 minutos intentando obtener respuesta.
    """
    
    # 🔥 Modelos priorizados por disponibilidad
    MODELS_PRIORITY = [
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.2-3b-instruct:free",
        "microsoft/phi-3-mini-128k-instruct:free",
        "qwen/qwen-2-7b-instruct:free",
        "anthropic/claude-3-haiku:free",
    ]
    
    payload_base = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.0,
        "max_tokens": 512,
    }
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": YOUR_SITE_URL,
        "X-Title": YOUR_SITE_NAME,
    }
    
    start_time = asyncio.get_event_loop().time()
    max_total_time = 180  # 3 minutos
    
    modelo_actual_idx = 0
    intento_global = 0
    
    while (asyncio.get_event_loop().time() - start_time) < max_total_time:
        intento_global += 1
        
        model_name = MODELS_PRIORITY[modelo_actual_idx % len(MODELS_PRIORITY)]
        payload = {**payload_base, "model": model_name}
        
        if intento_global > 1:
            delay = min(intento_global * 2, 30)
            print(f"⏳ Esperando {delay}s antes de reintentar (intento {intento_global})...")
            await asyncio.sleep(delay)
        
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                response = await client.post(
                    OPENROUTER_URL,
                    headers=headers,
                    json=payload
                )
            
            if not response.content:
                print(f"⚠️ {model_name}: Respuesta vacía")
                modelo_actual_idx += 1
                continue
            
            if response.status_code == 429:
                print(f"⚠️ {model_name}: Rate limit, probando otro modelo...")
                modelo_actual_idx += 1
                continue
            
            if response.status_code == 404:
                print(f"⚠️ {model_name}: No disponible")
                modelo_actual_idx += 1
                continue
            
            if response.status_code == 401:
                return "ERROR: API Key inválida"
            
            if response.status_code == 503:
                print(f"⚠️ {model_name}: Servicio no disponible")
                continue
            
            if response.status_code != 200:
                print(f"⚠️ {model_name}: HTTP {response.status_code}")
                modelo_actual_idx += 1
                continue
            
            try:
                response_data = response.json()
            except json.JSONDecodeError:
                print(f"⚠️ {model_name}: JSON inválido")
                modelo_actual_idx += 1
                continue
            
            if 'choices' not in response_data or not response_data['choices']:
                error_msg = response_data.get('error', {}).get('message', 'Sin detalles')
                print(f"⚠️ {model_name}: {error_msg}")
                modelo_actual_idx += 1
                continue
            
            result = response_data['choices'][0]['message']['content'].strip()
            
            if intento_global > 1:
                print(f"✅ Éxito con {model_name} (intento {intento_global})")
            
            return result
            
        except httpx.TimeoutException:
            print(f"⏱️ {model_name}: Timeout")
            continue
        
        except httpx.RequestError:
            print(f"⚠️ {model_name}: Error de red")
            await asyncio.sleep(5)
            continue
        
        except Exception as e:
            print(f"⚠️ {model_name}: {type(e).__name__}")
            modelo_actual_idx += 1
            continue
    
    elapsed = int(asyncio.get_event_loop().time() - start_time)
    print(f"\n❌ No se pudo obtener respuesta después de {elapsed}s y {intento_global} intentos")
    
    return "ERROR: Los servicios de IA están sobrecargados. Intenta en 5-10 minutos."
    

# --- 4. DEFINICIÓN DE AGENTES ---

@default_subscription
class Consultor(RoutedAgent):
    """Agente que maneja consultas de información: facturas específicas, deudas y estadísticas."""
    
    def __init__(self, sheets_service) -> None:
        super().__init__("Consultor de información.")
        self.sheets_service = sheets_service
    
    def _obtener_info_factura(self, factura_id: str) -> dict:
        """Obtiene información detallada de una factura específica."""
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, 
                range=SHEETS_RANGE
            ).execute()
            
            values = result.get('values', [])
            cuotas = []
            
            for i, row in enumerate(values):
                if i == 0: continue
                if len(row) >= 8:
                    cuota_id = str(row[1]).strip()
                    if cuota_id.startswith(f"{factura_id}-") or cuota_id == factura_id:
                        cuotas.append({
                            'cuota_id': cuota_id,
                            'monto_total': float(row[2]) if row[2] else 0,
                            'monto_pendiente': float(row[3]) if row[3] else 0,
                            'monto_cuota': float(row[4]) if row[4] else 0,
                            'fecha_vencimiento': row[5],
                            'tipo': row[6],
                            'estado': row[7]
                        })
            
            return {'existe': len(cuotas) > 0, 'cuotas': cuotas}
        except Exception as e:
            return {'existe': False, 'cuotas': [], 'error': str(e)}
    
    def _obtener_deudas_pendientes(self) -> list:
        """Obtiene todas las deudas pendientes."""
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, 
                range=SHEETS_RANGE
            ).execute()
            
            values = result.get('values', [])
            deudas = []
            
            for i, row in enumerate(values):
                if i == 0: continue
                if len(row) >= 8 and row[7] == 'PENDIENTE':
                    deudas.append({
                        'cuota_id': row[1],
                        'monto_pendiente': float(row[3]) if row[3] else 0,
                        'fecha_vencimiento': row[5]
                    })
            
            return deudas
        except Exception as e:
            return []
    
    def _obtener_estadisticas(self) -> dict:
        """Obtiene estadísticas generales de pagos."""
        try:
            # Deudas pendientes
            result_deuda = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, 
                range=SHEETS_RANGE
            ).execute()
            
            values_deuda = result_deuda.get('values', [])
            
            total_pendiente = 0
            cuotas_pendientes = 0
            facturas_unicas = set()
            
            for i, row in enumerate(values_deuda):
                if i == 0: continue
                if len(row) >= 8 and row[7] == 'PENDIENTE':
                    total_pendiente += float(row[3]) if row[3] else 0
                    cuotas_pendientes += 1
                    factura_base = row[1].split('-')[0]
                    facturas_unicas.add(factura_base)
            
            # Historial de pagos
            historial_sheet = 'Historial de Pagos' if 'Historial de Pagos' in ['Historial de Pagos'] else 'Facturas Pagadas'
            result_historial = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=f'{historial_sheet}!A:F'
            ).execute()
            
            values_historial = result_historial.get('values', [])
            
            total_pagado = 0
            num_transacciones = 0
            
            for i, row in enumerate(values_historial):
                if i == 0: continue
                if len(row) >= 4:
                    total_pagado += float(row[3]) if row[3] else 0
                    num_transacciones += 1
            
            return {
                'total_pendiente': total_pendiente,
                'cuotas_pendientes': cuotas_pendientes,
                'facturas_activas': len(facturas_unicas),
                'total_pagado': total_pagado,
                'num_transacciones': num_transacciones
            }
        except Exception as e:
            return {
                'total_pendiente': 0,
                'cuotas_pendientes': 0,
                'facturas_activas': 0,
                'total_pagado': 0,
                'num_transacciones': 0,
                'error': str(e)
            }
    
    @message_handler
    async def handle_message(self, message: PaymentMessage, ctx: MessageContext) -> None:
        consulta_tipo = message.data.get('consulta_tipo')
        
        if consulta_tipo == 'FACTURA_ESPECIFICA':
            factura_id = message.data.get('numero_factura')
            info = self._obtener_info_factura(factura_id)
            
            if info['existe']:
                print(f"\n📋 INFORMACIÓN DE FACTURA {factura_id}")
                print("="*70)
                
                total_general = info['cuotas'][0]['monto_total'] if info['cuotas'] else 0
                total_pendiente = sum(c['monto_pendiente'] for c in info['cuotas'])
                
                print(f"💰 Monto total: ${total_general:,.0f} COP")
                print(f"💵 Total pendiente: ${total_pendiente:,.0f} COP")
                print(f"📊 Cuotas: {len(info['cuotas'])}")
                print(f"\n{'Cuota':<15} {'Monto Cuota':<20} {'Pendiente':<20} {'Vencimiento':<15} {'Estado'}")
                print("-"*90)
                
                for cuota in info['cuotas']:
                    print(f"{cuota['cuota_id']:<15} ${cuota['monto_cuota']:>12,.0f} COP  ${cuota['monto_pendiente']:>12,.0f} COP  {cuota['fecha_vencimiento']:<15} {cuota['estado']}")
                
                print("="*70 + "\n")
            else:
                print(f"\n❌ No se encontró la factura {factura_id}\n")
        
        elif consulta_tipo == 'DEUDAS_PENDIENTES':
            deudas = self._obtener_deudas_pendientes()
            
            if deudas:
                print(f"\n💳 DEUDAS PENDIENTES")
                print("="*70)
                
                total = sum(d['monto_pendiente'] for d in deudas)
                print(f"📊 Total de cuotas pendientes: {len(deudas)}")
                print(f"💰 Monto total adeudado: ${total:,.0f} COP")
                print(f"\n{'Cuota ID':<20} {'Monto Pendiente':<25} {'Fecha Vencimiento'}")
                print("-"*70)
                
                # Ordenar por fecha de vencimiento
                deudas_ordenadas = sorted(deudas, key=lambda x: x['fecha_vencimiento'])
                
                for deuda in deudas_ordenadas:
                    print(f"{deuda['cuota_id']:<20} ${deuda['monto_pendiente']:>15,.0f} COP     {deuda['fecha_vencimiento']}")
                
                print("="*70 + "\n")
            else:
                print(f"\n✅ ¡No hay deudas pendientes!\n")
        
        elif consulta_tipo == 'ESTADISTICAS':
            stats = self._obtener_estadisticas()
            
            print(f"\n📈 ESTADÍSTICAS GENERALES")
            print("="*70)
            print(f"💳 Facturas activas: {stats['facturas_activas']}")
            print(f"📊 Cuotas pendientes: {stats['cuotas_pendientes']}")
            print(f"💰 Total pendiente: ${stats['total_pendiente']:,.0f} COP")
            print(f"\n✅ Total pagado: ${stats['total_pagado']:,.0f} COP")
            print(f"📝 Transacciones realizadas: {stats['num_transacciones']}")
            print("="*70 + "\n")


# En main.py, REEMPLAZA la función handle_message del Organizador con esta versión:

@default_subscription
class Organizador(RoutedAgent):
    """Agente central: Decide la ruta de comunicación y extrae datos clave usando la IA."""
    def __init__(self) -> None:
        super().__init__("Organizador central de pagos.")
        self._intent_prompt = self._build_intent_prompt()
        self._data_extraction_prompt = self._build_data_extraction_prompt()

    def _build_intent_prompt(self):
        return (
            "Eres un clasificador de intención. Analiza la petición del usuario y responde SOLO con "
            "UNA de las siguientes palabras (sin explicaciones adicionales):\n\n"
            "PLANIFICAR - Si el usuario quiere crear/registrar una nueva factura o dividirla en cuotas\n"
            "PAGAR - Si el usuario está reportando un pago, abono o cancelación de una deuda\n"
            "CONSULTA_FACTURA - Si pregunta por información específica de una factura\n"
            "CONSULTA_DEUDAS - Si pregunta por todas sus deudas o un resumen general\n"
            "CONSULTA_ESTADISTICAS - Si pide estadísticas o métricas generales\n\n"
            "Ejemplos:\n"
            "- 'ingresame la factura 15744 por $150000 en 3 cuotas' → PLANIFICAR\n"
            "- 'pagué $50000 de la factura 123' → PAGAR\n"
            "- 'consultar factura 456' → CONSULTA_FACTURA\n"
            "- 'ver mis deudas' → CONSULTA_DEUDAS\n\n"
            "Responde SOLO con la palabra clave, nada más."
        )

    def _build_data_extraction_prompt(self):
        return (
            "Extrae la siguiente información del texto y devuelve SOLO un objeto JSON válido.\n\n"
            "Campos a extraer:\n"
            "- numero_factura: el número de factura (string, sin ceros a la izquierda)\n"
            "- monto_total: monto total si es planificación (float, sin símbolos)\n"
            "- monto_abono: monto del pago/abono (float, sin símbolos)\n"
            "- fracciones: número de cuotas/fracciones (integer)\n"
            "- cuota_especifica: número de cuota específica si se menciona (integer o null)\n\n"
            "REGLAS IMPORTANTES:\n"
            "1. Extrae números SIN modificar: '15744' debe ser '15744', NO '1574'\n"
            "2. Para montos usa SOLO números: '$150000' → 150000.0\n"
            "3. Si dice 'pesos' o 'COP', ignóralos, solo extrae el número\n"
            "4. Si no menciona algo, usa: 0.0 para montos, 1 para fracciones, null para cuota\n"
            "5. NO incluyas texto extra, SOLO el JSON\n\n"
            "Ejemplos:\n"
            "Input: 'factura 15744 por $150000 pesos a 3 cuotas'\n"
            "Output: {\"numero_factura\": \"15744\", \"monto_total\": 150000.0, \"monto_abono\": 0.0, \"fracciones\": 3, \"cuota_especifica\": null}\n\n"
            "Input: 'pagué $50000 de la factura 123'\n"
            "Output: {\"numero_factura\": \"123\", \"monto_total\": 0.0, \"monto_abono\": 50000.0, \"fracciones\": 1, \"cuota_especifica\": null}\n\n"
            "Input: 'abono de $30000 a cuota 2 de factura 456'\n"
            "Output: {\"numero_factura\": \"456\", \"monto_total\": 0.0, \"monto_abono\": 30000.0, \"fracciones\": 1, \"cuota_especifica\": 2}\n\n"
            "Ahora extrae del siguiente texto y devuelve SOLO el JSON:"
        )

    @message_handler
    async def handle_message(self, message: PaymentMessage, ctx: MessageContext) -> None:
        print(f"\n{'='*60}")
        print(f"🤖 Procesando: '{message.user_input[:50]}...'")
        print(f"{'='*60}")

        if message.status == "INITIAL":
            # 1. Extraer intención
            print(f"🔄 Llamando a OpenRouter para detectar intención...")
            print(f"⏳ Esto puede tardar 30-60s si hay mucha demanda...")
            intent_response = await call_openrouter(self._intent_prompt, message.user_input)
            
            # 🔥 NUEVO: Verificar si hay error de OpenRouter
            if intent_response.startswith("ERROR:"):
                print(f"❌ OpenRouter falló: {intent_response}")
                print(f"💡 Los servicios gratuitos están saturados. Intenta en 5-10 minutos.")
                return
            
            lines = intent_response.strip().upper().split('\n')
            clean_intent = "DESCONOCIDO"

            for line in lines:
                words = line.split()
                if words and words[0] in ["PLANIFICAR", "PAGAR", "CONSULTA_FACTURA", "CONSULTA_DEUDAS", "CONSULTA_ESTADISTICAS"]:
                    clean_intent = words[0]
                    break
            
            print(f"🎯 Intención detectada: {clean_intent}")

            if clean_intent == "DESCONOCIDO":
                print(f"❌ No pude entender tu solicitud.")
                print(f"💡 Ejemplo: 'Factura 12345 por $500000 en 3 cuotas'")
                print(f"📝 Respuesta de OpenRouter: {intent_response[:200]}")
                return
            
            # 2. Extraer datos si es necesario
            if clean_intent in ["PLANIFICAR", "PAGAR"]:
                print(f"🔄 Extrayendo datos del mensaje...")
                data_json_str = await call_openrouter(
                    self._data_extraction_prompt, 
                    message.user_input
                )
                
                # 🔥 NUEVO: Verificar error antes de parsear
                if data_json_str.startswith("ERROR:"):
                    print(f"❌ OpenRouter falló en extracción: {data_json_str}")
                    print(f"💡 Intenta de nuevo en unos segundos")
                    return
                
                print(f"📦 Respuesta de extracción: {data_json_str[:200]}")
                
                try:
                    # Limpiar respuesta (remover markdown, espacios, etc.)
                    data_json_clean = data_json_str.strip()
                    
                    # Si viene con ```json o similar, limpiarlo
                    if '```' in data_json_clean:
                        import re
                        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', data_json_clean, re.DOTALL)
                        if json_match:
                            data_json_clean = json_match.group(1)
                        else:
                            json_match = re.search(r'\{.*\}', data_json_clean, re.DOTALL)
                            if json_match:
                                data_json_clean = json_match.group(0)
                    
                    # Reemplazar comillas simples por dobles
                    data_json_clean = data_json_clean.replace("'", '"')
                    
                    # Parsear JSON
                    data_ext = json.loads(data_json_clean)
                    message.data.update(data_ext)
                    
                    if clean_intent == "PLANIFICAR":
                        message.data["monto_pendiente"] = message.data["monto_total"]
                    
                    # Validar datos extraídos
                    factura_extraida = message.data['numero_factura']
                    monto_extraido = message.data.get('monto_total' if clean_intent == "PLANIFICAR" else 'monto_abono')
                    
                    print(f"✅ Datos extraídos correctamente:")
                    print(f"   📋 Factura: {factura_extraida}")
                    print(f"   💵 Monto: ${monto_extraido:,.0f} COP")
                    
                    if clean_intent == "PLANIFICAR":
                        print(f"   📊 Cuotas: {message.data['fracciones']}")
                    
                    # Validar que tenga datos mínimos
                    if factura_extraida == 'N/A' or monto_extraido == 0:
                        print(f"❌ Datos incompletos. Verifica el formato.")
                        print(f"💡 Ejemplo: 'Factura 12345 por $500000 en 3 cuotas'")
                        return
                
                except json.JSONDecodeError as e:
                    print(f"⚠️ Error al parsear JSON: {e}")
                    print(f"📝 Respuesta recibida: {data_json_str[:300]}")
                    print(f"❌ No se pudieron extraer los datos. Intenta reformular.")
                    return
                except Exception as e:
                    print(f"⚠️ Error inesperado: {e}")
                    print(f"❌ No se pudieron procesar los datos.")
                    return
            
            elif clean_intent in ["CONSULTA_FACTURA", "CONSULTA_DEUDAS", "CONSULTA_ESTADISTICAS"]:
                if clean_intent == "CONSULTA_FACTURA":
                    import re
                    numeros = re.findall(r'\d+', message.user_input)
                    if numeros:
                        message.data['numero_factura'] = numeros[0]
                
                consulta_map = {
                    "CONSULTA_FACTURA": "FACTURA_ESPECIFICA",
                    "CONSULTA_DEUDAS": "DEUDAS_PENDIENTES",
                    "CONSULTA_ESTADISTICAS": "ESTADISTICAS"
                }
                message.data['consulta_tipo'] = consulta_map[clean_intent]
            
            # 3. Crear mensaje actualizado y enrutar
            next_message = message.model_copy(update={
                "intent": clean_intent,
                "status": "INTENT_CLASSIFIED"
            })
            
            if clean_intent == "PLANIFICAR":
                await self.send_message(next_message, AgentId("planificador", "default"))
            
            elif clean_intent == "PAGAR":
                await self.send_message(next_message, AgentId("registrador", "default"))
            
            elif clean_intent in ["CONSULTA_FACTURA", "CONSULTA_DEUDAS", "CONSULTA_ESTADISTICAS"]:
                await self.send_message(next_message, AgentId("consultor", "default"))

        elif message.status == "PLANNED":
            print(f"✅ Planificación completada: {message.data.get('fracciones')} cuotas creadas")
            await self.send_message(message, AgentId("notificador", "default"))
            await self.send_message(message, AgentId("registrador", "default"))


@default_subscription
class Planificador(RoutedAgent):
    """Agente que determina la fecha óptima y calcula las fechas fraccionadas."""
    def __init__(self, sheets_service) -> None:
        super().__init__("Planificador de fechas de pago.")
        self.sheets_service = sheets_service
        
    def _redondear_pesos_colombianos(self, monto: float) -> int:
        """
        Redondea al múltiplo de 50 más cercano (moneda válida en Colombia).
        Ejemplos: 266666.67 → 266650, 133333.33 → 133350
        """
        return int(round(monto / 50) * 50)
    
    def _obtener_fechas_ocupadas(self) -> dict:
        """
        Obtiene todas las fechas de vencimiento ya programadas y cuenta cuántas hay por día.
        Retorna un diccionario: {'2025-12-15': 3, '2025-12-16': 1, ...}
        """
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, 
                range='Deuda Pendiente!F:F'  # Columna F: Fecha Vencimiento
            ).execute()
            
            values = result.get('values', [])
            fechas_count = {}
            
            for i, row in enumerate(values):
                if i == 0:  # Saltar encabezado
                    continue
                if row and row[0]:
                    fecha = _normalize_sheet_date(row[0])
                    if fecha:
                        fechas_count[fecha] = fechas_count.get(fecha, 0) + 1
            
            # Solo mostrar si hay fechas ocupadas
            if fechas_count:
                print(f"📊 Fechas ocupadas: {len(fechas_count)} día(s) con pagos programados")
            return fechas_count
            
        except Exception as e:
            return {}
    
    def _encontrar_fecha_disponible(self, fecha_base: date, fechas_ocupadas: dict, max_por_dia: int = 2) -> str:
        """
        Encuentra la fecha más cercana que tenga MENOS pagos programados.
        Estrategia: Prioriza fechas vacías, luego con 1 pago, luego con 2, etc.
        """
        fecha_candidata = fecha_base
        intentos = 0
        max_intentos = 45  # Buscar hasta 45 días después
        
        # Estrategia: Buscar primero fechas completamente vacías
        mejor_fecha = None
        menor_ocupacion = float('inf')
        
        while intentos < max_intentos:
            fecha_str = fecha_candidata.strftime("%Y-%m-%d")
            ocupacion = fechas_ocupadas.get(fecha_str, 0)
            
            # Si encontramos una fecha vacía, la usamos inmediatamente
            if ocupacion == 0:
                if fecha_str != fecha_base.strftime("%Y-%m-%d"):
                    print(f"   ↪ Ajustada a {fecha_str} (fecha vacía)")
                return fecha_str
            
            # Si no está vacía pero tiene menos pagos que lo que hemos visto, la guardamos
            if ocupacion < menor_ocupacion and ocupacion < max_por_dia:
                mejor_fecha = fecha_str
                menor_ocupacion = ocupacion
            
            # Probar el día siguiente
            fecha_candidata += timedelta(days=1)
            intentos += 1
        
        # Si no encontramos fecha vacía, usar la que tenga menos pagos
        if mejor_fecha:
            print(f"   ⚠️ {mejor_fecha} (ya tiene {menor_ocupacion} pago(s), pero es la mejor opción)")
            return mejor_fecha
        
        # Si todas las fechas están llenas, devolver la fecha base original
        print(f"   ⚠️ Sin fechas disponibles cercanas. Usando fecha base.")
        return fecha_base.strftime("%Y-%m-%d")
        
    @message_handler
    async def handle_message(self, message: PaymentMessage, ctx: MessageContext) -> None:
        fracciones = message.data['fracciones']
        monto_total = message.data['monto_total']
        fecha_base = datetime.strptime(message.data['fecha_actual'], "%Y-%m-%d")
        
        # 🟢 NUEVO: Obtener fechas ya ocupadas
        fechas_ocupadas = self._obtener_fechas_ocupadas()
        
        # 🟢 CORRECCIÓN 1: Calcular monto por cuota con redondeo a múltiplos de 50
        monto_fraccion_exacto = monto_total / fracciones
        monto_fraccion_redondeado = self._redondear_pesos_colombianos(monto_fraccion_exacto)
        
        # Ajustar la última cuota para compensar diferencias de redondeo
        suma_cuotas = monto_fraccion_redondeado * (fracciones - 1)
        ultima_cuota = monto_total - suma_cuotas
        
        # Crear lista de montos por cuota
        montos_por_cuota = [monto_fraccion_redondeado] * (fracciones - 1) + [ultima_cuota]
        
        # 🟢 CORRECCIÓN 2: Distribución inteligente de fechas
        fechas_pago = []
        
        for i in range(1, fracciones + 1):
            # Calcular fecha base (30 días por cuota)
            fecha_objetivo = fecha_base + timedelta(days=30 * i)
            
            # Encontrar fecha disponible cercana
            fecha_disponible = self._encontrar_fecha_disponible(
                fecha_objetivo.date(), 
                fechas_ocupadas,
                max_por_dia=3  # Máximo 3 pagos por día
            )
            
            fechas_pago.append(fecha_disponible)
            
            # Actualizar contador para la siguiente iteración
            fechas_ocupadas[fecha_disponible] = fechas_ocupadas.get(fecha_disponible, 0) + 1
        
        message.data['fechas_pago'] = fechas_pago
        message.data['montos_por_cuota'] = montos_por_cuota
        message.data['monto_fraccionado'] = monto_fraccion_redondeado  # Para compatibilidad
        message.status = "PLANNED"
        
        # Mostrar resumen al usuario
        print(f"📅 Cuotas calculadas:")
        for i, (fecha, monto) in enumerate(zip(fechas_pago, montos_por_cuota), 1):
            print(f"   Cuota {i}: ${monto:,.0f} COP - Vence: {fecha}")
        
        if suma_cuotas != monto_total:
            print(f"   ℹ️  Última cuota ajustada para compensar redondeos")
        
        await self.send_message(message, AgentId("organizador", "default"))


@default_subscription
class Notificador(RoutedAgent):
    """Agente que gestiona Google Calendar para recordatorios."""
    
    def __init__(self, calendar_service) -> None: 
        super().__init__("Notificador de Eventos y Tareas.")
        self.calendar_service = calendar_service 
        self.calendar_id = 'primary'

    def _get_task_title(self, cuota_id: str, monto_pendiente: float) -> str:
        """Genera título mostrando solo el monto pendiente total."""
        factura_id, cuota_num = cuota_id.split('-')
        if monto_pendiente <= 0:
            return f"✅ PAGO COMPLETADO - Factura {factura_id}, Cuota {cuota_num}"
        return f"💰 PAGO PENDIENTE - Factura {factura_id}, Cuota {cuota_num}: ${monto_pendiente:,.0f} COP"

    @message_handler
    async def handle_message(self, message: PaymentMessage, ctx: MessageContext) -> None:
        
        factura_id = message.data.get('numero_factura')
        
        if message.intent == "PLANIFICAR":
            
            fechas_pago = message.data.get('fechas_pago', [])
            monto_fraccionado = message.data.get('monto_fraccionado')
            
            for i, fecha_pago_str in enumerate(fechas_pago):
                cuota_id = f"{factura_id}-{i+1}"
                montos_por_cuota = message.data.get('montos_por_cuota', [])
                monto_pendiente = montos_por_cuota[i] if i < len(montos_por_cuota) else monto_fraccionado 
                
                try:
                    time_min = datetime.strptime(fecha_pago_str, '%Y-%m-%d').isoformat() + 'Z' 
                    time_max = (datetime.strptime(fecha_pago_str, '%Y-%m-%d') + timedelta(days=1)).isoformat() + 'Z'
                    
                    events_result = self.calendar_service.events().list(
                        calendarId=self.calendar_id, 
                        timeMin=time_min, 
                        timeMax=time_max, 
                        q=cuota_id,
                        singleEvents=True, 
                        orderBy='startTime'
                    ).execute()
                    
                    events = events_result.get('items', [])
                    
                    if events:
                        continue
                
                except Exception as e:
                    pass
                
                self._create_or_update_task(cuota_id, monto_fraccionado, fecha_pago_str)
        
        elif message.intent == "PAGAR" and message.status == "POST_ABONO":
            
            cuota_id = message.data.get('cuota_id')
            monto_pendiente_nuevo = message.data.get('monto_pendiente_simulado', 0.0)
            fecha_pago_original = message.data.get('fecha_pago_original')
            
            if not cuota_id:
                 return
            
            if not fecha_pago_original:
                fecha_pago_original = datetime.now().strftime('%Y-%m-%d')

            eventos_eliminados = 0
            try:
                fecha_base = datetime.strptime(fecha_pago_original, '%Y-%m-%d')
                time_min = (fecha_base - timedelta(days=90)).isoformat() + 'Z'
                time_max = (fecha_base + timedelta(days=90)).isoformat() + 'Z'
                
                events_result = self.calendar_service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    maxResults=250
                ).execute()
                
                all_events = events_result.get('items', [])
                
                events_to_delete = []
                factura_id_base, cuota_num = cuota_id.split('-')
                
                for event in all_events:
                    summary = event.get('summary', '')
                    description = event.get('description', '')
                    
                    match_cuota_id = cuota_id in summary or cuota_id in description
                    match_factura_cuota = (
                        f"Factura {factura_id_base}" in summary and f"Cuota {cuota_num}" in summary
                    ) or (
                        f"Factura {factura_id_base}" in description and f"#{cuota_num}" in description
                    )
                    
                    if match_cuota_id or match_factura_cuota:
                        events_to_delete.append(event)
                
                for event in events_to_delete:
                    try:
                        event_id = event['id']
                        
                        self.calendar_service.events().delete(
                            calendarId=self.calendar_id, 
                            eventId=event_id
                        ).execute()
                        
                        eventos_eliminados += 1
                    except Exception as e_del:
                        pass
                
            except Exception as e:
                pass
            
            if monto_pendiente_nuevo > 0:
                self._create_or_update_task(cuota_id, monto_pendiente_nuevo, fecha_pago_original)
                print(f"📅 Recordatorio actualizado en Google Calendar")
            else:
                print(f"📅 Recordatorio eliminado de Google Calendar")

    def _create_or_update_task(self, cuota_id, monto_pendiente_nuevo, fecha_pago):
        """Crea recordatorio con cuota_id explícito en descripción."""
        title_new = self._get_task_title(cuota_id, monto_pendiente_nuevo)
        factura_id, cuota_num = cuota_id.split('-')
        
        event = {
            'summary': title_new, 
            'description': f"[ID: {cuota_id}] Pago de cuota #{cuota_num} de Factura {factura_id}. Monto PENDIENTE: ${monto_pendiente_nuevo:,.0f} COP.",
            'start': {'date': fecha_pago}, 
            'end': {'date': fecha_pago},
            'reminders': {
                'useDefault': False,
                'overrides': [{'method': 'email', 'minutes': 24 * 60}],
            },
        }
        
        try:
            self.calendar_service.events().insert(
                calendarId=self.calendar_id, 
                body=event
            ).execute()
        except Exception as e:
            pass


@default_subscription
class Registrador(RoutedAgent):
    """
    Agente que gestiona Google Sheets para registrar, verificar y actualizar 
    el estado de las facturas.
    """
    
    def __init__(self, sheets_service) -> None: 
        super().__init__("Registrador de Sheets.")
        self.sheets_service = sheets_service
        self.sheet_ids = self._get_sheet_ids()
        self.facturas_existentes = self._load_facturas_from_sheets()
        self.facturas_procesadas = set()

    def _get_sheet_ids(self):
        """Obtiene los IDs de todas las hojas en el Spreadsheet."""
        try:
            spreadsheet = self.sheets_service.spreadsheets().get(
                spreadsheetId=SPREADSHEET_ID
            ).execute()
            
            sheet_ids = {}
            for sheet in spreadsheet.get('sheets', []):
                sheet_title = sheet['properties']['title']
                sheet_id = sheet['properties']['sheetId']
                sheet_ids[sheet_title] = sheet_id
            
            return sheet_ids
            
        except HttpError as e:
            return {}

    def _load_facturas_from_sheets(self):
        """Carga datos de la hoja 'Deuda Pendiente' y los formatea."""
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, range=SHEETS_RANGE).execute()
            
            values = result.get('values', [])
            
            facturas = {}
            if values and len(values) > 1: 
                for row in values[1:]: 
                    try:
                        if len(row) < 8: continue
                        
                        factura_id = str(row[1]).strip()
                        monto_pendiente = float(row[3]) 
                        estado = row[7].strip()

                        if factura_id and estado == "PENDIENTE":
                            facturas[factura_id] = {"monto_pendiente": monto_pendiente, "estado": estado}
                    except (IndexError, ValueError):
                        continue
            
            return facturas
            
        except HttpError as e:
            return {}
        except Exception as e:
            return {}

    def _find_factura_row(self, factura_id: str):
        """
        Busca una factura (o cuota) por ID en 'Deuda Pendiente' y devuelve el número de fila.
        """
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, range=SHEETS_RANGE).execute()
            values = result.get('values', [])
            
            for i, row in enumerate(values):
                if i == 0: continue

                if len(row) > 7:
                    sheet_id = str(row[1]).strip()
                    estado = row[7].strip()
                    row_number = i + 1

                    if sheet_id == factura_id:
                        return row_number, row
                    
                    if sheet_id.startswith(f"{factura_id}-") and estado == "PENDIENTE":
                        return row_number, row
            
            return None, None
        
        except HttpError as e:
            return None, None

    def _registrar_pago_en_historial(self, factura_id: str, tipo_transaccion: str, monto_abonado: float, monto_pendiente_restante: float, notas: str = ""):
        """Registra en la hoja correcta con columnas en orden."""
        try:
            historial_sheet_name = None
            if 'Historial de Pagos' in self.sheet_ids:
                historial_sheet_name = 'Historial de Pagos'
            elif 'Facturas Pagadas' in self.sheet_ids:
                historial_sheet_name = 'Facturas Pagadas'
            else:
                return
            
            nueva_fila_historial = [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                factura_id,
                tipo_transaccion,
                monto_abonado,
                monto_pendiente_restante,
                notas
            ]
            
            self.sheets_service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f'{historial_sheet_name}!A:F',
                valueInputOption='USER_ENTERED',
                body={'values': [nueva_fila_historial]}
            ).execute()
            
            print(f"📊 Registrado en historial: {tipo_transaccion} - ${monto_abonado:,.0f} COP")
            
        except HttpError as e:
            pass

    @message_handler
    async def handle_message(self, message: PaymentMessage, ctx: MessageContext) -> None:
        factura_id: str | None = message.data.get('numero_factura')

        if factura_id is None or factura_id == "N/A":
             print("❌ No se especificó un número de factura válido")
             return

        mensaje_unico = f"{factura_id}_{message.intent}_{message.status}_{message.data.get('monto_abono', 0)}"
        if mensaje_unico in self.facturas_procesadas:
            return
        self.facturas_procesadas.add(mensaje_unico)

        if message.intent == "PLANIFICAR":
            
            facturas_similares = [k for k in self.facturas_existentes.keys() if k == factura_id or k.startswith(f"{factura_id}-")]
            
            if facturas_similares:
                print(f"⚠️ La factura {factura_id} ya existe en el sistema")
                return 
            
            monto_total = message.data['monto_total']
            monto_fraccionado = message.data.get('monto_fraccionado', monto_total)
            fracciones = message.data['fracciones']
            fechas_pago = message.data['fechas_pago']
            montos_por_cuota = message.data.get('montos_por_cuota', [monto_fraccionado] * fracciones)
            
            rows_to_append = []
            
            for i, fecha_pago_str in enumerate(fechas_pago):
                monto_cuota = montos_por_cuota[i] if i < len(montos_por_cuota) else monto_fraccionado
                
                new_row = [
                    datetime.now().strftime('%Y-%m-%d'), 
                    f"{factura_id}-{i+1}",               
                    monto_total,                         
                    monto_cuota,                         
                    monto_cuota,                         
                    fecha_pago_str,                      
                    "Fraccionado" if fracciones > 1 else "Total",
                    "PENDIENTE"                          
                ]
                rows_to_append.append(new_row)
            
            try:
                self.sheets_service.spreadsheets().values().append(
                    spreadsheetId=SPREADSHEET_ID,
                    range='Deuda Pendiente!A:H', 
                    valueInputOption='USER_ENTERED',
                    body={'values': rows_to_append}
                ).execute()
                print(f"✅ Factura {factura_id} registrada en Google Sheets ({fracciones} cuota{'s' if fracciones > 1 else ''})")
                
                # Actualizar caché con montos correctos por cuota
                for i in range(fracciones):
                    cuota_id = f"{factura_id}-{i+1}"
                    monto_cuota = montos_por_cuota[i] if i < len(montos_por_cuota) else monto_fraccionado
                    self.facturas_existentes[cuota_id] = {"monto_pendiente": monto_cuota, "estado": "PENDIENTE"} 
                
                self.facturas_existentes[factura_id] = {"monto_pendiente": monto_total, "estado": "PLANIFICADO"}

            except HttpError as e:
                print(f"❌ Error al registrar en Sheets")
                
        
        elif message.intent == "PAGAR":
            
            monto_abono = message.data.get('monto_abono', 0.0)
            cuota_especifica = message.data.get('cuota_especifica')
            
            if factura_id in self.facturas_existentes and self.facturas_existentes[factura_id].get('estado') == 'PAGADA':
                print(f"⚠️ La factura {factura_id} ya fue pagada completamente")
                return
            
            factura_id_a_buscar = factura_id
            
            if cuota_especifica:
                cuota_objetivo = f"{factura_id}-{cuota_especifica}"
                if cuota_objetivo in self.facturas_existentes and self.facturas_existentes[cuota_objetivo].get('estado') == 'PENDIENTE':
                    factura_id_a_buscar = cuota_objetivo
                    print(f"🎯 Procesando cuota específica: {cuota_objetivo}")
                else:
                    print(f"⚠️ La cuota {cuota_objetivo} no existe o ya fue pagada")
                    return
            else:
                if factura_id not in self.facturas_existentes:
                    related_cuotas = [k for k in self.facturas_existentes.keys() if k.startswith(f"{factura_id}-")]
                    
                    if related_cuotas:
                        factura_id_a_buscar = related_cuotas[0]

            if factura_id_a_buscar not in self.facturas_existentes:
                
                print(f"⚠️ La factura {factura_id} no existe en el sistema")
                
                if monto_abono > 0.0:
                    print(f"📝 Registrando pago de factura no planificada")
                    
                    self._registrar_pago_en_historial(
                        factura_id,
                        "Pago Sin Planificación",
                        monto_abono,
                        0.0,
                        f"Pago de factura no registrada previamente. Monto: ${monto_abono:.2f}"
                    )
                    
                    self.facturas_existentes[factura_id] = {"monto_pendiente": 0.0, "estado": "PAGADA"}
                    
                    print(f"✅ Pago de ${monto_abono:,.0f} COP registrado")
                    return
                else:
                    print(f"❌ No se puede procesar sin monto de pago")
                    return
            
            if monto_abono > 0.0:
                
                monto_restante_por_aplicar = monto_abono
                cuotas_procesadas = []
                
                factura_base = factura_id_a_buscar.split('-')[0]
                
                if cuota_especifica:
                    cuotas_a_procesar = [factura_id_a_buscar]
                    todas_cuotas = sorted([
                        k for k in self.facturas_existentes.keys() 
                        if k.startswith(f"{factura_base}-") and 
                        self.facturas_existentes[k].get('estado') == 'PENDIENTE'
                    ])
                    idx_actual = todas_cuotas.index(factura_id_a_buscar) if factura_id_a_buscar in todas_cuotas else -1
                    if idx_actual >= 0:
                        cuotas_a_procesar.extend(todas_cuotas[idx_actual + 1:])
                else:
                    cuotas_a_procesar = sorted([
                        k for k in self.facturas_existentes.keys() 
                        if k.startswith(f"{factura_base}-") and 
                        self.facturas_existentes[k].get('estado') == 'PENDIENTE'
                    ])
                
                for cuota_actual in cuotas_a_procesar:
                    if monto_restante_por_aplicar <= 0:
                        break
                    
                    row_number, current_row = self._find_factura_row(cuota_actual)
                    
                    if not row_number or current_row is None:
                        continue
                    
                    try:
                        if len(current_row) <= 3: continue
                        monto_pendiente_actual = float(current_row[3])
                    except (ValueError):
                        continue
                    
                    monto_a_aplicar = min(monto_restante_por_aplicar, monto_pendiente_actual)
                    monto_pendiente_nuevo = monto_pendiente_actual - monto_a_aplicar
                    monto_pendiente_nuevo_redondeado = round(monto_pendiente_nuevo, 2)
                    
                    factura_completa_id_sheets = current_row[1]
                    
                    if factura_completa_id_sheets in self.facturas_existentes:
                        self.facturas_existentes[factura_completa_id_sheets]['monto_pendiente'] = monto_pendiente_nuevo_redondeado
                    
                    self._registrar_pago_en_historial(
                        factura_completa_id_sheets,
                        "Abono Parcial" if monto_pendiente_nuevo_redondeado > 0 else "Pago Completo",
                        monto_a_aplicar,
                        monto_pendiente_nuevo_redondeado,
                        f"Abono de ${monto_a_aplicar:.2f}. Monto anterior: ${monto_pendiente_actual:.2f}"
                    )
                    
                    if monto_pendiente_nuevo_redondeado > 0:
                        new_value = [[monto_pendiente_nuevo_redondeado]] 
                        range_to_update = f'Deuda Pendiente!D{row_number}' 
                        
                        try:
                            self.sheets_service.spreadsheets().values().update(
                                spreadsheetId=SPREADSHEET_ID, range=range_to_update,
                                valueInputOption='USER_ENTERED', body={'values': new_value}
                            ).execute()
                            print(f"✅ Cuota {factura_completa_id_sheets}: ${monto_pendiente_actual:,.0f} → ${monto_pendiente_nuevo_redondeado:,.0f} COP")
                            
                        except HttpError as e:
                            pass
                        
                        try:
                            cuota_id_completo = current_row[1]
                            fecha_pago_original = _normalize_sheet_date(current_row[5])
                            if not fecha_pago_original:
                                fecha_pago_original = datetime.now().strftime('%Y-%m-%d')
                            
                            mensaje_notificador = message.model_copy(update={
                                "status": "POST_ABONO",
                                "data": {
                                    **message.data,
                                    "cuota_id": cuota_id_completo,
                                    "fecha_pago_original": fecha_pago_original,
                                    "monto_pendiente_simulado": monto_pendiente_nuevo_redondeado
                                }
                            })
                            
                            await self.send_message(mensaje_notificador, AgentId("notificador", "default"))
                            
                        except (IndexError, ValueError) as e:
                            pass
                    
                    else:
                        try:
                            deuda_sheet_id = self.sheet_ids.get('Deuda Pendiente', 0)
                            
                            if deuda_sheet_id is None:
                                continue
                            
                            requests = [{'deleteDimension': {'range': {
                                'sheetId': deuda_sheet_id,
                                'dimension': 'ROWS', 
                                'startIndex': row_number - 1, 
                                'endIndex': row_number
                            }}}]
                            
                            self.sheets_service.spreadsheets().batchUpdate(
                                spreadsheetId=SPREADSHEET_ID, body={'requests': requests}
                            ).execute()
                            
                            print(f"✅ Cuota {factura_completa_id_sheets} PAGADA COMPLETAMENTE")
                            
                            self.facturas_existentes[factura_completa_id_sheets] = {"monto_pendiente": 0.0, "estado": "PAGADA"}
                            
                            try:
                                cuota_id_completo = current_row[1]
                                fecha_pago_original = _normalize_sheet_date(current_row[5])
                                
                                mensaje_notificador = message.model_copy(update={
                                    "status": "POST_ABONO",
                                    "data": {
                                        **message.data,
                                        "cuota_id": cuota_id_completo,
                                        "fecha_pago_original": fecha_pago_original or datetime.now().strftime('%Y-%m-%d'),
                                        "monto_pendiente_simulado": 0.0
                                    }
                                })
                                
                                await self.send_message(mensaje_notificador, AgentId("notificador", "default"))
                                
                            except (IndexError, ValueError) as e:
                                pass
                        
                        except HttpError as e:
                            pass
                    
                    monto_restante_por_aplicar -= monto_a_aplicar
                    cuotas_procesadas.append({
                        'cuota': factura_completa_id_sheets,
                        'aplicado': monto_a_aplicar,
                        'restante': monto_pendiente_nuevo_redondeado
                    })
                
                if monto_restante_por_aplicar > 0:
                    print(f"⚠️ Excedente de ${monto_restante_por_aplicar:,.0f} COP - No hay más cuotas pendientes")
                
                print(f"✅ Pago procesado: {len(cuotas_procesadas)} cuota(s) afectada(s)")
                    
            elif "completada" in message.user_input.lower() or "pagada" in message.user_input.lower():
                 print(f"⚠️ Para marcar como pagada, debe especificar el monto del pago")


# --- 5. FUNCIÓN PRINCIPAL - MODO CHATBOT ---

def mostrar_menu():
    """Muestra el menú de ayuda del chatbot."""
    print("\n" + "="*70)
    print("💡 COMANDOS DISPONIBLES:")
    print("="*70)
    print("📝 PLANIFICAR: 'Tengo factura [número] por $[monto] COP en [N] cuotas'")
    print("💰 PAGAR:      'Pagué $[monto] COP de la factura [número]'")
    print("💵 ABONO:      'Aboné $[monto] COP a la cuota [N] de factura [número]'")
    print("\n🔍 CONSULTAS:")
    print("📋 INFO:       'Consultar factura [número]' o 'Info de factura [número]'")
    print("💳 DEUDAS:     'Ver deudas pendientes' o 'Mostrar todas las deudas'")
    print("📈 STATS:      'Estadísticas' o 'Ver estadísticas de pagos'")
    print("\n🛠️  UTILIDADES:")
    print("🧹 LIMPIAR:    'limpiar hoja' (elimina TODAS las facturas pendientes)")
    print("\n❓ AYUDA:      Escribe 'ayuda' o 'comandos'")
    print("🚪 SALIR:      Escribe 'salir', 'exit' o 'q'")
    print("="*70)
    print("💵 NOTA: Todos los montos se redondean a múltiplos de 50 COP")
    print("📌 TIP: El sistema prioriza fechas vacías para evitar sobrecargas")
    print("="*70 + "\n")

async def chatbot_loop(runtime, sheets_service):
    """Bucle principal del chatbot interactivo."""
    
    print("\n" + "="*70)
    print("🤖 ASISTENTE DE GESTIÓN DE PAGOS")
    print("="*70)
    print("¡Hola! Estoy aquí para ayudarte a gestionar tus facturas y pagos.")
    mostrar_menu()
    
    while True:
        try:
            # Entrada del usuario
            user_input = input("🗣️  Tú: ").strip()
            
            if not user_input:
                continue
            
            # Comandos especiales
            if user_input.lower() in ['salir', 'exit', 'q', 'quit']:
                print("\n👋 ¡Hasta luego! Tus datos están guardados en Google Sheets y Calendar.")
                break
            
            if user_input.lower() in ['ayuda', 'help', 'comandos', '?']:
                mostrar_menu()
                continue
            
            if user_input.lower() in ['sheets', 'ver sheets', 'link']:
                print(f"\n🔗 Tu Google Sheets: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")
                continue
            
            # Comando para limpiar hoja
            if user_input.lower() in ['limpiar', 'limpiar hoja', 'borrar todo', 'reset']:
                print("\n⚠️  ¿ESTÁS SEGURO? Esto eliminará TODAS las facturas pendientes.")
                confirmacion = input("Escribe 'SI CONFIRMO' para continuar: ").strip()
                
                if confirmacion == "SI CONFIRMO":
                    try:
                        # Obtener todas las filas
                        result = sheets_service.spreadsheets().values().get(
                            spreadsheetId=SPREADSHEET_ID,
                            range='Deuda Pendiente!A:H'
                        ).execute()
                        
                        values = result.get('values', [])
                        num_filas = len(values)
                        
                        if num_filas > 1:  # Si hay más que solo el encabezado
                            # Eliminar todas las filas excepto el encabezado
                            requests = [{
                                'deleteDimension': {
                                    'range': {
                                        'sheetId': 0,  # ID de la primera hoja
                                        'dimension': 'ROWS',
                                        'startIndex': 1,  # Desde la segunda fila (índice 1)
                                        'endIndex': num_filas
                                    }
                                }
                            }]
                            
                            sheets_service.spreadsheets().batchUpdate(
                                spreadsheetId=SPREADSHEET_ID,
                                body={'requests': requests}
                            ).execute()
                            
                            print(f"✅ Se eliminaron {num_filas - 1} factura(s) pendiente(s).")
                        else:
                            print("ℹ️  No hay facturas pendientes para eliminar.")
                    except Exception as e:
                        print(f"❌ Error al limpiar: {e}")
                else:
                    print("❌ Operación cancelada.")
                continue
            
            # Procesar mensaje
            print("\n⏳ Procesando...")
            
            data_mensaje = {"user_input": user_input}
            
            await runtime.send_message(
                PaymentMessage.model_validate(data_mensaje), 
                AgentId("organizador", "default")
            )
            
            await runtime.stop_when_idle()
            runtime.start()
            
            print("\n" + "-"*70)
            
        except KeyboardInterrupt:
            print("\n\n👋 Interrupción detectada. ¡Hasta luego!")
            break
        except Exception as e:
            print(f"\n❌ Error inesperado: {e}")
            print("Por favor, intenta de nuevo.")

async def main() -> None:
    runtime = SingleThreadedAgentRuntime()

    print("\n🔐 Iniciando autenticación de Google...")
    
    calendar_service = obtener_credenciales_google('Calendar')
    sheets_service = obtener_credenciales_google('Sheets')
    
    if not calendar_service or not sheets_service:
        print("❌ Error: No se pudieron inicializar los servicios de Google.")
        return 
    
    global SPREADSHEET_ID

    if SPREADSHEET_ID == 'TU_ID_DE_HOJA_DE_CALCULO' or not os.path.exists('sheets_id.txt'):
        print("⚠️ Configuración de Sheets: ID no encontrado")
        
        if os.path.exists('sheets_id.txt'):
            with open('sheets_id.txt', 'r') as f:
                SPREADSHEET_ID = f.read().strip()
                print(f"✅ Usando ID guardado")
        else:
            print("⏳ Creando nueva hoja de cálculo...")
            
            new_id, new_url = crear_hoja_calculo(
                sheets_service, 
                f"Gestor de Pagos - {datetime.now().strftime('%Y-%m-%d')}"
            )

            if new_id:
                SPREADSHEET_ID = new_id
                with open('sheets_id.txt', 'w') as f:
                    f.write(new_id)
                print(f"✅ Hoja creada. Guarda este link: {new_url}")
            else:
                print("❌ No se pudo crear la hoja de cálculo")
                return

    # Registrar agentes
    await Organizador.register(runtime, "organizador", Organizador)
    await Planificador.register(runtime, "planificador", lambda: Planificador(sheets_service))
    await Notificador.register(runtime, "notificador", lambda: Notificador(calendar_service))
    await Registrador.register(runtime, "registrador", lambda: Registrador(sheets_service))
    await Consultor.register(runtime, "consultor", lambda: Consultor(sheets_service))
    
    runtime.start()

    print(f"\n✅ Sistema iniciado correctamente")
    print(f"📊 Google Sheets ID: {SPREADSHEET_ID}")
    print(f"📅 Google Calendar: {CALENDAR_ID}")
    
    # Iniciar chatbot
    await chatbot_loop(runtime, sheets_service)

if __name__ == "__main__":
    asyncio.run(main())