
import json
import os
from jinja2 import Environment, FileSystemLoader

# --- CONFIGURATION: MOCK ORDER OPTIONS ---
ORDER_OPTIONS = {
    # TRUE = Include in report, FALSE = Exclude
    "include_common_trisomies": True,      # T13, T18, T21
    "include_additional_trisomies": True,  # T9, T16, T22
    "include_other_autosomal": True,       # "All Other Autosomal Trisomies"
    "include_sex_chromosomes": True,       # XO, XXX, XXY, XYY
    "include_common_microdeletions": True, # Panel of 8 (Page 1)
    "include_comprehensive_microdeletions": True # Full list (Page 3)
}

# --- DATA DEFINITIONS ---
COMMON_TRISOMIES = [
    {"condition": "Trisomy 21 (Down Syndrome)", "result": "Low Risk"},
    {"condition": "Trisomy 18 (Edwards Syndrome)", "result": "Low Risk"},
    {"condition": "Trisomy 13 (Patau Syndrome)", "result": "Low Risk"},
]

ADDITIONAL_TRISOMIES = [
    {"condition": "Trisomy 9", "result": "Low Risk"},
    {"condition": "Trisomy 16", "result": "Low Risk"},
    {"condition": "Trisomy 22", "result": "Low Risk"},
]

OTHER_AUTOSOMAL = [
    {"condition": "All Other Autosomal Trisomy", "result": "Low Risk"},
]

SCA_CONDITIONS = [
    {"condition": "XO (Turner Syndrome)", "result": "Low Risk"},
    {"condition": "XXX (Trisomy X)", "result": "Low Risk"},
    {"condition": "XXY (Klinefelter Syndrome)", "result": "Low Risk"},
    {"condition": "XYY (Jacob Syndrome)", "result": "Low Risk"},
]

COMMON_MICRODELETIONS = [
    {"condition": "1p36 deletion syndrome", "result": "Low Risk"},
    {"condition": "Wolf-Hirschhorn syndrome", "result": "Low Risk"},
    {"condition": "Williams-Beuren syndrome", "result": "Low Risk"},
    {"condition": "Prader-willi/Angelman syndrome", "result": "Low Risk"},
    {"condition": "Cri Du Chat syndrome", "result": "Low Risk"},
    {"condition": "Jacobsen syndrome", "result": "Low Risk"},
    {"condition": "DiGeorge syndrome (22q11.2)", "result": "Low Risk"},
    {"condition": "2q33.1 deletion syndrome", "result": "Low Risk"},
]

# Full 141+ List (Page 3)
COMPREHENSIVE_MICRODELETIONS = [
    "1p36 terminal region (includes GABRD) gain", "Partial trisomy 5p", "Partial trisomy 12p", "17q11.2 recurrent region (includes NF1) gain",
    "1q21.1 recurrent region (distal, BP3-BP4) (includes GJA5) loss", "Partial monosomy 5q", "Partial trisomy 12q", "17q11.2 recurrent region (includes NF1) loss",
    "1q21.1 recurrent region (distal, BP3-BP4) (includes GJA5) gain", "Partial trisomy 5q", "Monosomy 13q14", "17q12 recurrent (RCAD syndrome) region (includes HNF1B) gain",
    "1q43-q44 terminal region (includes AKT3)", "6q24 region (includes PLAGL1)", "Monosomy 13q21-qter", "17q12 recurrent (RCAD syndrome) region (includes HNF1B) loss",
    "Partial monosomy 1p", "Partial trisomy 6p", "Trisomy 13cen-q14", "17q21.31 recurrent region (includes KANSL1)",
    "Monosomy 1q21-q32", "Partial monosomy 6q", "Trisomy 13q21-qter", "17q23.1-q23.2 recurrent region (includes TBX2, TBX4) gain",
    "Monosomy 1q42-qter", "Partial trisomy 6q", "14q11.2 region including CHD8 and SUPT16H", "17q23.1-q23.2 recurrent region (includes TBX2, TBX4) loss",
    "Trisomy 1q23-qter", "7p22.1 region (includes ACTB)", "DLK1-MEG3 Intergenic Region", "SOX9 upstream enhancer region",
    "2p15-p16.1 region (includes BCL11A)", "7q11.23 recurrent (Williams-Beuren syndrome) region (includes ELN) gain", "Partial trisomy 14", "Trisomy 17pter-q21",
    "2p24.3 MYCN-DDX1 duplication region", "7q11.23 recurrent distal region (includes HIP1, YWHAG)", "Trisomy 14q24-qter", "Partial monosomy 18p",
    "2q11.2 recurrent region (includes ARID5A, TMEM127)", "7q36.3 ZRS (SHH cis-regulatory) duplication region (within LMBR1 intron 5)", "15q11.2-q13 recurrent (PWS/AS) region (Class I, BP1-BP3 and Class II, BP2-BP3) gain", "Partial tetrasomy 18p",
    "2q13 recurrent region (distal) (includes BCL2L11) gain", "Partial monosomy 7p", "15q13.3 recurrent region (BP4-BP5) (includes CHRNA7)", "Trisomy 18pter-q12",
    "2q13 recurrent region (distal) (includes BCL2L11) loss", "Partial trisomy 7p", "15q13.3 recurrent region (D-CHRNA7 to BP5) (includes CHRNA7, OTUD7A)", "Monosomy 18q21-qter",
    "Partial monosomy 2p", "Monosomy 7q32-qter", "15q24 recurrent region (LCR A-LCR C)", "Trisomy 18q12-qter",
    "Partial trisomy 2p", "Trisomy 7q21-q31", "15q24 recurrent region (LCR A-LCR D) (includes SIN3A)", "Partial monosomy 20p",
    "Partial monosomy 2q", "Trisomy 7q32-qter", "15q24 recurrent region (LCR C-LCR D) (includes SIN3A)", "Trisomy 20p",
    "Partial trisomy 2q", "8p23.1 recurrent region (includes GATA4) gain", "15q25.2 recurrent region (proximal, LCR-A/B-C) (includes RPS17)", "Partial monosomy 21",
    "3q24 Region (includes ZIC1)", "8p23.1 recurrent region (includes GATA4) loss", "Supernumerary inv dup (15)", "22q11.2 recurrent (DGS) region (proximal, A-B, A-C, or A-D) (includes TBX1) gain",
    "3q29 recurrent region (includes DLG1)", "Partial trisomy 8q", "Trisomy 15q22-qter", "22q11.2 recurrent region (central, B-D or C-D) (includes CRKL)",
    "Monosomy 3p11-p21", "Partial monosomy 9p", "16p11.2 recurrent region (distal, BP2-BP3) (includes SH2B1)", "22q11.2 recurrent region (distal type I, D-E or D-F)",
    "Monosomy 3p25-pter", "Partial trisomy 9p", "16p11.2 recurrent region (proximal, BP4-BP5) (includes TBX6) gain", "22q11.2 recurrent region (distal type III, D-G, D-H) (includes SMARCB1)",
    "Partial trisomy 3p", "Partial monosomy 9q", "16p11.2 recurrent region (proximal, BP4-BP5) (includes TBX6) loss", "22q11.2 recurrent region (distal type III, E-H or F-H) (includes SMARCB1)",
    "Partial monosomy 3q", "Partial trisomy 9q", "16p12.2 recurrent region (proximal) (includes EEF2K, CDR2)", "22q11.2 recurrent region (distal type III, F-G) (includes SMARCB1)",
    "Partial trisomy 3q", "10q22.3-q23.2 recurrent region (includes BMPR1A)", "16p13.11 recurrent region (BP1-BP3, BP2-BP3, or BP2-BP4) (includes MYH11) gain", "22q11.21 recurrent (CES) region (includes CECR2)",
    "Partial monosomy 3p", "Partial monosomy 10p", "16p13.11 recurrent region (BP1-BP3, BP2-BP3, or BP2-BP4) (includes MYH11) loss", "Xp22.31 recurrent region (includes STS)",
    "4p16.3 terminal (Wolf-Hirschhorn syndrome) region gain", "Partial trisomy 10p", "16p13.3 region (includes CREBBP)", "Xp21.2 region (includes NROB1)",
    "Partial monosomy 4p", "Partial monosomy 10q", "Partial trisomy 16p", "Xp11.23 region (includes MAOA and MAOB)",
    "Partial trisomy 4p", "Partial trisomy 10q", "Partial monosomy 16q", "Xp11.22-p11.23 recurrent region (includes SHROOM4)",
    "Monosomy 4q21-q31", "11p11.2 (Potocki-Shaffer syndrome) region (includes ALX4, EXT2)", "Partial trisomy 16q", "Xp11.22 region (includes HUWE1)",
    "Monosomy 4q31-qter", "11p13 (WAGR syndrome) region", "17p11.2 recurrent (SMS/PLS) region (includes RAI1) gain", "Xq25 region (includes STAG2) gain",
    "Partial trisomy 4q", "11q13.2-q13.4 recurrent region (includes SHANK2, FGF3)", "17p11.2 recurrent (SMS/PLS) region (includes RAI1) loss", "Xq25 region (includes STAG2) loss",
    "Partial monosomy 4p", "Partial trisomy 11p", "17p12 recurrent (HNPP/CMT1A) region (includes PMP22) gain", "Xq28 recurrent region (includes GDI1)",
    "5p15 terminal (Cri du chat syndrome) region gain", "Partial monosomy 11q", "17p12 recurrent (HNPP/CMT1A) region (includes PMP22) loss", "Xq28 recurrent region (int22h1/int22h2-flanked) (includes RAB39B) gain",
    "5q35 recurrent (Sotos syndrome) region (includes NSD1) gain", "Partial trisomy 11q", "17p13.3 (Miller-Dieker syndrome) region (includes YWHAE and PAFAH1B1) gain", "Xq28 recurrent region (int22h1/int22h2-flanked) (includes RAB39B) loss",
    "5q35 recurrent (Sotos syndrome) region (includes NSD1) loss", "Partial monosomy 12p", "17p13.3 (Miller-Dieker syndrome) region (includes YWHAE and PAFAH1B1) loss", "Xq28 region (includes MECP2) gain",
    "","","","Xq28 region (includes MECP2) loss"
]

# Base Data
data = {
    "Patient_Name": "Jane Doe",
    "DOB": "1990-01-01",
    "W": "12", "D": "3",
    "Pregnancy_Type": "Singleton",
    "MRN": "MRN-123456",
    "Indication": "Advanced Maternal Age",
    "Sample_Number": "S-123456",
    "Doctor": "Dr. Smith",
    "Hospital": "General Hospital",
    "Draw": "2023-10-01",
    "Report": "2023-10-05",
    "Order_ID": "ORD-7890",
    "Result_MDResult": "Low Risk",
    "Gender": "Female",
    "FF_YFF": "10.5%",
    "Interpretation_MDI": "This screening test indicates a low risk for the common aneuploidies and microdeletion syndromes evaluated. No evidence of fetal chromosome aneuploidy or the targeted microdeletion/duplication syndromes was detected. Fetal fraction was adequate for analysis. This is a screening test, not diagnostic; confirmatory testing is recommended if clinical suspicion remains.",
}

def chunk_list(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def _write_pdf(html_content, pdf_filename):
    try:
        import sys
        if sys.platform == 'darwin':
            os.environ['DYLD_FALLBACK_LIBRARY_PATH'] = '/opt/homebrew/lib:/usr/local/lib:' + os.environ.get('DYLD_FALLBACK_LIBRARY_PATH', '')
        from weasyprint import HTML
        HTML(string=html_content, base_url='.').write_pdf(pdf_filename)
        print(f"PDF generated: {pdf_filename}")
        return True
    except ImportError:
        return False
    except Exception as e:
        print(f"PDF generation failed ({pdf_filename}): {e}")
        return False

def generate_report(options=None, custom_data=None):
    # Setup Jinja2 Environment
    file_loader = FileSystemLoader('.')
    env = Environment(loader=file_loader)
    template = env.get_template('GX_Report_Template.html')

    # Determine Options (Merge provided options with defaults)
    current_options = ORDER_OPTIONS.copy()
    if options:
        current_options.update(options)

    # Prepare Data
    current_data = data.copy()
    if custom_data:
        # Recursive update or simple shallow update? Shallow is safer for top-level keys.
        current_data.update(custom_data)

    # --- DYNAMIC DATA CONSTRUCTION ---
    # 1. Aneuploidies List
    common_trisomies = []
    other_aneuploidies = []

    # Separate Common Trisomies (Ordered First, 1 per row)
    if current_options["include_common_trisomies"]:
        common_trisomies.extend(COMMON_TRISOMIES)

    # Collect others (Ordered After, 2 per row)
    if current_options["include_additional_trisomies"]:
        other_aneuploidies.extend(ADDITIONAL_TRISOMIES)
    if current_options["include_other_autosomal"]:
        other_aneuploidies.extend(OTHER_AUTOSOMAL)
    if current_options["include_sex_chromosomes"]:
        other_aneuploidies.extend(SCA_CONDITIONS)

    # 2. Microdeletions (Page 1)
    microdeletions_page1_list = []
    if current_options["include_common_microdeletions"]:
        microdeletions_page1_list.extend(COMMON_MICRODELETIONS)

    # 3. Microdeletions (Page 3)
    microdeletions_page3_list = []
    show_page3 = False
    if current_options["include_comprehensive_microdeletions"]:
        microdeletions_page3_list.extend(COMPREHENSIVE_MICRODELETIONS)
        # Add "Other..." to Page 1 if Comp. Panel is ordered
        microdeletions_page1_list.append({"condition": "Other microdeletions/duplications", "result": "Low Risk"})
        show_page3 = True

    # --- LIST CHUNKING ---
    aneuploidy_rows = []
    
    # Part 1: Trisomies (Vertical Split)
    left_stack = []
    if current_options["include_common_trisomies"]:
        left_stack.extend(COMMON_TRISOMIES)
    if current_options["include_other_autosomal"]:
        left_stack.extend(OTHER_AUTOSOMAL)
        
    # Right: Additional
    right_stack = []
    if current_options["include_additional_trisomies"]:
        right_stack.extend(ADDITIONAL_TRISOMIES)
        
    # SCA Logic:
    # If NO Additional Trisomies (Right Stack Free) -> SCAs go to Right Stack (Vertical Fill next to Common)
    # Else -> SCAs go to Bottom (Standard 2x2 Fill)
    sca_bottom_list = []
    
    if current_options["include_sex_chromosomes"]:
        if not current_options["include_additional_trisomies"]:
            right_stack.extend(SCA_CONDITIONS)
        else:
            sca_bottom_list.extend(SCA_CONDITIONS)
        
    # Zip Left and Right
    import itertools
    for left, right in itertools.zip_longest(left_stack, right_stack):
        row = []
        if left:
            row.append(left)
        else:
            # Placeholder for empty left cell (if logic requires filler)
             # The template handles < 2 items by adding a filler th/td.
             # BUT if we have [RightItem] only, it renders as Left.
             # So we MUST strictly control the list structure: [LeftItem, RightItem] or [Empty, RightItem]
             pass
        
        # We need a robust row structure for the loop: {% for item in row %}
        # The template just iterates. item 0 -> Col 1. item 1 -> Col 2.
        # So we must ensure list has 2 items if utilizing right column.
        
        l_item = left if left else {"condition": "", "result": ""}
        r_item = right if right else None 
        current_row = []
        if left:
            current_row.append(left)
        else:
            # If we have a Right item but no Left, we need a placeholder in slot 0
            if right:
                current_row.append({"condition": "", "result": ""})
        
        if right:
            current_row.append(right)
            
        aneuploidy_rows.append(current_row)

    # Append separate SCA block if applicable
    if sca_bottom_list:
        aneuploidy_rows.extend(list(chunk_list(sca_bottom_list, 2)))

    microdeletion_page1_rows = list(chunk_list(microdeletions_page1_list, 2))
    
    # Page 3 Chunking (Only if needed)
    microdeletion_page3_rows = []
    if show_page3:
        microdeletion_page3_rows = list(chunk_list(microdeletions_page3_list, 4))

    # Calculate Total Pages
    total_pages = 3 if show_page3 else 2

    # Render Template
    output = template.render(
        **current_data,
        aneuploidy_rows=aneuploidy_rows,
        microdeletion_page1_rows=microdeletion_page1_rows,
        microdeletion_page3_rows=microdeletion_page3_rows,
        show_page3=show_page3,
        total_pages=total_pages
    )

    # Save output
    output_filename = 'GX_Report_Sample.html'
    with open(output_filename, 'w', encoding='utf-8') as f:
        f.write(output)
    _write_pdf(output, 'GX_Report_Sample.pdf')

    try:
        import shutil
        shutil.copy2(output_filename, 'GX_Report_Generated.html')
        if os.path.isfile('GX_Report_Sample.pdf'):
            shutil.copy2('GX_Report_Sample.pdf', 'GX_Report_Generated.pdf')
    except OSError:
        pass
    
    # Return output filename for server usage
    return output_filename

if __name__ == "__main__":
    generate_report()
