from flask import Flask, request, jsonify, send_file, render_template_string
import os
import generate_report

app = Flask(__name__)

# Basic Config
HTML_FILE = 'GX_Report_Generated.html'

@app.route('/')
def index():
    # Helper to read the interface HTML
    try:
        with open('test_interface.html', 'r') as f:
            return f.read()
    except FileNotFoundError:
        return "test_interface.html not found. Please ensure it exists."

@app.route('/generate', methods=['POST'])
def generate():
    try:
        req_payload = request.json
        options = req_payload.get('options')
        custom_data = req_payload.get('custom_data')
        
        print(f"Received generation request. Options: {options}, Custom Data: {bool(custom_data)}")
        generate_report.generate_report(options=options, custom_data=custom_data)
        return jsonify({"status": "success", "message": "Report generated successfully"})
    except Exception as e:
        print(f"Error generating report: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/view_report')
def view_report():
    if os.path.exists(HTML_FILE):
        return send_file(HTML_FILE)
    else:
        return "Report not found. Please click Generate first."

@app.route('/genolyx_logo.png')
def serve_logo():
    if os.path.exists('genolyx_logo.png'):
        return send_file('genolyx_logo.png')
    else:
        return "Logo not found", 404

if __name__ == '__main__':
    print("Starting Test Server on port 5000...")
    print("Access at: http://localhost:5000")
    app.run(debug=True, port=5000)
