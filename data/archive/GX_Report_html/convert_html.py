import sys
import os

if sys.platform == 'darwin':
    os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = '/opt/homebrew/lib:/usr/local/lib:' + os.environ.get('DYLD_FALLBACK_LIBRARY_PATH', '')

from weasyprint import HTML

def convert_html(input_file, output_file):
    print(f"Converting {input_file} to {output_file}...")
    HTML(input_file).write_pdf(output_file)
    print("PDF generation complete.")

if __name__ == "__main__":
    convert_html('GX_Report_Generated.html', 'GX_Report_Generated.pdf')
