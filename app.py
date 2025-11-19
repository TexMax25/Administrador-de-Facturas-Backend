from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import os
from datetime import datetime
from threading import Lock
import sys
import io

# Importar tus módulos existentes
import main

app = Flask(__name__)
CORS(app)  # Permitir peticiones desde GitHub Pages

runtime_lock = Lock()

@app.route('/api/chat', methods=['POST'])
def chat():
    """Endpoint que recibe configuración del usuario en cada request"""
    try:
        # Obtener datos del request
        data = request.json
        user_message = data.get('message', '').strip()
        
        # Obtener configuración del usuario desde headers
        api_key = request.headers.get('X-API-Key')
        sheets_id = request.headers.get('X-Sheets-ID')
        
        if not user_message:
            return jsonify({'success': False, 'message': '❌ Mensaje vacío'}), 400
        
        if not api_key or not sheets_id:
            return jsonify({'success': False, 'message': '❌ Faltan credenciales'}), 401
        
        # Configurar variables globales temporalmente para este request
        main.OPENROUTER_API_KEY = api_key
        main.SPREADSHEET_ID = sheets_id
        
        # Procesar mensaje con runtime limpio
        with runtime_lock:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(procesar_mensaje_usuario(user_message))
                if isinstance(result, tuple):
                    text, html = result
                else:
                    text = result
                    html = None
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except:
                    pass
                try:
                    loop.close()
                except:
                    pass
                asyncio.set_event_loop(None)
        
        return jsonify({'success': True, 'message': text, 'html': html})
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': f'❌ Error: {str(e)}'}), 500


async def procesar_mensaje_usuario(user_input: str):
    """Procesa cada mensaje con un runtime limpio"""
    from autogen_core import AgentId, SingleThreadedAgentRuntime
    
    # Crear runtime limpio
    local_runtime = SingleThreadedAgentRuntime()
    
    # Obtener servicios de Google (usa las credenciales locales del servidor)
    sheets_service = main.obtener_credenciales_google('Sheets')
    calendar_service = main.obtener_credenciales_google('Calendar')
    
    # Registrar agentes
    await main.Organizador.register(local_runtime, "organizador", main.Organizador)
    await main.Planificador.register(local_runtime, "planificador", lambda: main.Planificador(sheets_service))
    await main.Notificador.register(local_runtime, "notificador", lambda: main.Notificador(calendar_service))
    await main.Registrador.register(local_runtime, "registrador", lambda: main.Registrador(sheets_service))
    await main.Consultor.register(local_runtime, "consultor", lambda: main.Consultor(sheets_service))
    
    local_runtime.start()
    
    # Capturar output
    old_stdout = sys.stdout
    sys.stdout = buffer = io.StringIO()
    
    try:
        data_mensaje = {"user_input": user_input}
        mensaje = main.PaymentMessage.model_validate(data_mensaje)
        
        await local_runtime.send_message(mensaje, AgentId("organizador", "default"))
        await local_runtime.stop_when_idle()
        
        console_output = buffer.getvalue()
    except Exception as e:
        console_output = f"Error: {str(e)}"
        import traceback
        traceback.print_exc()
    finally:
        sys.stdout = old_stdout
        try:
            await local_runtime.stop()
        except:
            pass
    
    # Formatear respuesta (reutilizar tu función existente)
    from app import formatear_respuesta_procesada, generar_respuesta_contextual
    
    user_lower = user_input.lower()
    if any(cmd in user_lower for cmd in ['ayuda', 'help', 'sheets', 'calendar']):
        return generar_respuesta_contextual(user_input)
    
    return formatear_respuesta_procesada(user_input, console_output)


@app.route('/api/status', methods=['GET'])
def status():
    """Health check endpoint"""
    return jsonify({
        'status': 'online',
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/init-sheets', methods=['POST'])
def init_sheets():
    """Endpoint para crear una nueva hoja de cálculo para el usuario"""
    try:
        data = request.json
        user_email = data.get('email', 'usuario')
        
        sheets_service = main.obtener_credenciales_google('Sheets')
        
        spreadsheet_title = f"Gestor de Pagos - {user_email} - {datetime.now().strftime('%Y-%m-%d')}"
        new_id, new_url = main.crear_hoja_calculo(sheets_service, spreadsheet_title)
        
        if new_id:
            return jsonify({
                'success': True,
                'sheets_id': new_id,
                'sheets_url': new_url
            })
        else:
            return jsonify({'success': False, 'message': 'No se pudo crear la hoja'}), 500
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


if __name__ == '__main__':
    print("\n" + "="*70)
    print("🚀 BACKEND MULTIUSUARIO - GESTOR DE PAGOS")
    print("="*70)
    print("📡 Puerto: 5000")
    print("🌐 CORS habilitado para GitHub Pages")
    print("="*70 + "\n")
    
    # Desplegar en 0.0.0.0 para que sea accesible desde internet
    app.run(debug=True, port=5000, host='0.0.0.0')