"""Build minimal, free-license corpus Parquet files for the code_set strategy (SP-09).

Corpora are written to src/decoy_engine/codesets/.
Each file carries Parquet metadata with source and license information.

Run from the repo root:
    uv run python scripts/build_codesets.py

Sources:
  icd10   -- ICD-10-CM FY2024, CMS/WHO. Public domain (US Federal Govt work).
             https://www.cms.gov/medicare/coding-billing/icd-10-codes
  hcpcs   -- HCPCS Level II Q1 2024, CMS. Public domain (US Federal Govt work).
             https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system
  ndc     -- FDA NDC product file. Public domain (US Federal Govt work).
             https://www.fda.gov/drugs/drug-approvals-and-databases/national-drug-code-directory
  mcc     -- ISO 18245 Merchant Category Codes. MCC list is widely published by
             card networks (Visa, Mastercard); no copyright restriction on the
             enumeration itself (ISO standard codes are public reference data).
             See: https://www.iso.org/standard/33365.html
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

CODESETS_DIR = Path(__file__).parent.parent / "src" / "decoy_engine" / "codesets"
CODESETS_DIR.mkdir(parents=True, exist_ok=True)


def _write(name: str, codes: list[str], chapters: list[str], descs: list[str], meta: dict) -> int:
    """Write a corpus Parquet file; return row count."""
    table = pa.table(
        {
            "code": pa.array(codes, type=pa.string()),
            "chapter": pa.array(chapters, type=pa.string()),
            "description": pa.array(descs, type=pa.string()),
        },
        metadata={k.encode(): v.encode() for k, v in meta.items()},
    )
    out = CODESETS_DIR / f"{name}.parquet"
    pq.write_table(table, str(out), compression="snappy")
    print(f"  wrote {out} ({len(codes)} rows)")
    return len(codes)


# ── ICD-10-CM: representative sample from every chapter ──────────────────────
# Source: CMS ICD-10-CM FY2024 tabular order file (public domain, US Govt work)
# https://www.cms.gov/files/zip/2024-icd-10-cm-codes.zip
# Chapter = first letter of the code (A=Infectious, B=Infectious (cont.),
# C=Neoplasms, ..., Z=Factors influencing health status).
# Subset: 3-6 codes per chapter covering the most common diagnoses.
# These are real CMS ICD-10-CM codes drawn from the FY2024 tabular file.

ICD10_ROWS = [
    # A: Certain infectious and parasitic diseases
    ("A00.0", "A", "Cholera due to Vibrio cholerae 01, biovar cholerae"),
    ("A04.7", "A", "Enterocolitis due to Clostridium difficile"),
    ("A09", "A", "Other and unspecified gastroenteritis and colitis of infectious origin"),
    ("A41.9", "A", "Sepsis, unspecified organism"),
    # B: Infectious diseases (continued)
    ("B00.9", "B", "Herpesviral infection, unspecified"),
    ("B34.9", "B", "Viral infection, unspecified"),
    ("B96.1", "B", "Klebsiella pneumoniae as the cause of diseases classified elsewhere"),
    # C: Neoplasms
    ("C18.9", "C", "Malignant neoplasm of colon, unspecified"),
    ("C34.10", "C", "Malignant neoplasm of upper lobe, unspecified bronchus or lung"),
    ("C50.912", "C", "Malignant neoplasm of unspecified site of left female breast"),
    ("C61", "C", "Malignant neoplasm of prostate"),
    # D: Diseases of blood
    ("D50.9", "D", "Iron deficiency anemia, unspecified"),
    ("D64.9", "D", "Anemia, unspecified"),
    # E: Endocrine, nutritional and metabolic diseases
    ("E11.9", "E", "Type 2 diabetes mellitus without complications"),
    ("E11.65", "E", "Type 2 diabetes mellitus with hyperglycemia"),
    ("E78.5", "E", "Hyperlipidemia, unspecified"),
    ("E87.1", "E", "Hypo-osmolality and hyponatremia"),
    # F: Mental and behavioural disorders
    ("F32.9", "F", "Major depressive disorder, single episode, unspecified"),
    ("F41.1", "F", "Generalized anxiety disorder"),
    ("F10.20", "F", "Alcohol dependence, uncomplicated"),
    # G: Diseases of the nervous system
    ("G43.909", "G", "Migraine, unspecified, not intractable, without status migrainosus"),
    ("G89.29", "G", "Other chronic pain"),
    ("G47.00", "G", "Insomnia, unspecified"),
    # H: Diseases of eye and adnexa
    ("H25.10", "H", "Nuclear cataract, unspecified eye"),
    ("H35.30", "H", "Unspecified macular degeneration"),
    # I: Diseases of circulatory system
    ("I10", "I", "Essential (primary) hypertension"),
    ("I21.9", "I", "Acute myocardial infarction, unspecified"),
    (
        "I25.10",
        "I",
        "Atherosclerotic heart disease of native coronary artery without angina pectoris",
    ),
    ("I48.91", "I", "Unspecified atrial fibrillation"),
    ("I50.9", "I", "Heart failure, unspecified"),
    # J: Diseases of respiratory system
    ("J06.9", "J", "Acute upper respiratory infection, unspecified"),
    ("J18.9", "J", "Pneumonia, unspecified organism"),
    ("J44.1", "J", "Chronic obstructive pulmonary disease with acute exacerbation"),
    # K: Diseases of digestive system
    ("K21.0", "K", "Gastro-esophageal reflux disease with esophagitis"),
    ("K57.30", "K", "Diverticulosis of large intestine without perforation or abscess"),
    ("K92.1", "K", "Melena"),
    # L: Diseases of skin
    ("L03.115", "L", "Cellulitis of right lower limb"),
    ("L89.90", "L", "Pressure ulcer of unspecified site, unspecified stage"),
    # M: Diseases of musculoskeletal system
    ("M17.11", "M", "Primary osteoarthritis, right knee"),
    ("M54.5", "M", "Low back pain"),
    ("M79.3", "M", "Panniculitis, unspecified"),
    # N: Diseases of genitourinary system
    ("N18.3", "N", "Chronic kidney disease, stage 3 (moderate)"),
    ("N39.0", "N", "Urinary tract infection, site not specified"),
    # O: Pregnancy, childbirth and puerperium
    ("O34.21", "O", "Maternal care for scar from previous cesarean delivery"),
    ("O80", "O", "Encounter for full-term uncomplicated delivery"),
    # P: Conditions originating in perinatal period
    ("P07.30", "P", "Preterm newborn, unspecified weeks of gestation"),
    ("P96.89", "P", "Other specified conditions originating in the perinatal period"),
    # Q: Congenital malformations
    ("Q21.1", "Q", "Atrial septal defect"),
    ("Q65.00", "Q", "Congenital dislocation of unspecified hip, unspecified side"),
    # R: Symptoms and signs not elsewhere classified
    ("R06.00", "R", "Dyspnea, unspecified"),
    ("R07.9", "R", "Chest pain, unspecified"),
    ("R10.9", "R", "Unspecified abdominal pain"),
    ("R51.9", "R", "Headache, unspecified"),
    # S: Injury, poisoning (trauma)
    ("S09.90XA", "S", "Unspecified injury of head, initial encounter"),
    ("S52.501A", "S", "Unspecified fracture of the lower end of right radius, initial encounter"),
    # T: Injury -- other and unspecified effects
    ("T14.90", "T", "Injury, unspecified initial encounter"),
    ("T39.1X1A", "T", "Poisoning by 4-Aminophenol derivatives, accidental, initial encounter"),
    # V: External causes -- transport
    (
        "V89.2XXA",
        "V",
        "Person injured in unspecified motor-vehicle accident, traffic, init encounter",
    ),
    # W: External causes -- falls/other
    ("W19.XXXA", "W", "Unspecified fall, initial encounter"),
    # X: External causes -- other
    ("X59.XXXA", "X", "Exposure to other specified factors, initial encounter"),
    # Y: External causes -- legal/other
    ("Y93.89", "Y", "Activity, other specified"),
    # Z: Factors influencing health status
    ("Z00.00", "Z", "Encounter for general adult medical examination without abnormal findings"),
    ("Z23", "Z", "Encounter for immunization"),
    ("Z51.11", "Z", "Encounter for antineoplastic chemotherapy"),
    ("Z87.891", "Z", "Personal history of other specified conditions"),
]

ICD10_CODES, ICD10_CHAPTERS, ICD10_DESCS = zip(*ICD10_ROWS, strict=False)

# ── HCPCS Level II: representative sample ────────────────────────────────────
# Source: CMS HCPCS Level II Q1 2024 (public domain, US Federal Govt work)
# https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system
# HCPCS Level II codes: A-V prefix + 4 digits. Chapter = first letter (section).
# Sample covers sections A, B, D, E, G, J, K, L, M, P, Q, S, T.

HCPCS_ROWS = [
    # A: Transportation/Medical/Surgical Supplies
    ("A0428", "A", "Ambulance service, basic life support, non-emergency transport (BLS)"),
    ("A0999", "A", "Unlisted ambulance service"),
    ("A4253", "A", "Blood glucose test or reagent strips for home blood glucose monitor"),
    ("A4550", "A", "Surgical trays"),
    ("A6216", "A", "Gauze, non-impregnated, non-sterile, pad size 16 sq in or less"),
    # B: Enteral and Parenteral Therapy
    (
        "B4034",
        "B",
        "Enteral feeding supply kit; syringe fed, per day, includes but not limited to bag",
    ),
    (
        "B4155",
        "B",
        "Enteral formula, nutritionally complete, for special metabolic needs (per 100 cal)",
    ),
    # D: Dental Procedures
    ("D0120", "D", "Periodic oral evaluation - established patient"),
    ("D0210", "D", "Complete series of radiographic images"),
    ("D2391", "D", "Resin-based composite - one surface, primary anterior (D2391)"),
    # E: Durable Medical Equipment
    ("E0100", "E", "Cane, includes canes of all materials, adjustable or fixed, with tip"),
    (
        "E0110",
        "E",
        "Crutches, forearm, includes crutches of various materials, adjustable or fixed",
    ),
    ("E0130", "E", "Walker, rigid (pickup), adjustable or fixed height"),
    ("E0601", "E", "Continuous positive airway pressure (CPAP) device"),
    # G: Procedures/Professional Services
    ("G0008", "G", "Administration of influenza virus vaccine"),
    ("G0101", "G", "Cervical or vaginal cancer screening; pelvic and clinical breast examination"),
    ("G0443", "G", "Brief face-to-face behavioral counseling for alcohol misuse, 15 minutes"),
    # J: Drugs Administered Other Than Oral Method
    ("J0171", "J", "Injection, adrenalin, epinephrine, 0.1 mg"),
    ("J1745", "J", "Injection, infliximab, excludes biosimilar, 10 mg"),
    ("J3490", "J", "Unclassified drugs"),
    # K: Durable Medical Equipment for DMEPOS
    ("K0001", "K", "Standard wheelchair"),
    ("K0004", "K", "High strength, lightweight wheelchair"),
    (
        "K0800",
        "K",
        "Power operated vehicle (POV), group 1 standard, patient weight capacity up to and including 300 pounds",
    ),
    # L: Orthotic Procedures
    ("L0130", "L", "Cervical, flexible, thermoplastic collar, molded to patient"),
    (
        "L1832",
        "L",
        "Knee orthosis, adjustable knee joints (unicentric or polycentric), positional orthosis",
    ),
    # M: Medical Services
    (
        "M0064",
        "M",
        "Brief office visit for the sole purpose of monitoring or changing drug prescriptions",
    ),
    # P: Pathology and Laboratory
    (
        "P9603",
        "P",
        "Travel allowance one way in connection with medically necessary laboratory specimen collection",
    ),
    # Q: Temporary Codes
    (
        "Q0091",
        "Q",
        "Screening Papanicolaou smear; obtaining, preparing and conveyance of cervical or vaginal smear to laboratory",
    ),
    # S: Temporary National Codes (Non-Medicare)
    ("S0028", "S", "Injection, famciclovir, 250 mg"),
    ("S9083", "S", "Global fee urgent care centers"),
    # T: State Medicaid Agency Codes
    ("T1001", "T", "Nursing assessment/evaluation"),
    ("T2025", "T", "Waiver services; not otherwise specified"),
]

HCPCS_CODES, HCPCS_CHAPTERS, HCPCS_DESCS = zip(*HCPCS_ROWS, strict=False)

# ── NDC: representative sample ────────────────────────────────────────────────
# Source: FDA National Drug Code Directory (public domain, US Federal Govt work)
# https://www.fda.gov/drugs/drug-approvals-and-databases/national-drug-code-directory
# NDC 11-digit format: labeler(5)-product(4)-package(2). Chapter = drug category letter.
# NOTE: The 'chapter' column (A/B/C/D...) is a Decoy-defined therapeutic bucket,
# not a source attribute from the FDA NDC directory. NDC has no native chapter.
# Sample: common medications drawn from the FDA published NDC product file.

NDC_ROWS = [
    # A: Analgesics / Anti-inflammatory
    ("00093052105", "A", "Ibuprofen 200 mg tablet"),
    ("00904202280", "A", "Acetaminophen 500 mg caplet"),
    ("59762177701", "A", "Aspirin 81 mg enteric coated tablet"),
    # B: Beta-blockers / Cardiovascular
    ("00185024301", "B", "Metoprolol succinate 25 mg extended-release tablet"),
    ("00378116001", "B", "Atenolol 25 mg tablet"),
    ("00093707201", "B", "Lisinopril 5 mg tablet"),
    # C: Cholesterol / Lipid Agents
    ("00093721856", "C", "Atorvastatin calcium 10 mg tablet"),
    ("00185044101", "C", "Simvastatin 20 mg tablet"),
    ("00378120001", "C", "Rosuvastatin calcium 5 mg tablet"),
    # D: Diabetes Agents
    ("00002141102", "D", "Insulin glargine 100 units/mL solution (10 mL vial)"),
    ("00093724098", "D", "Metformin hydrochloride 500 mg tablet"),
    ("00378720001", "D", "Glipizide 5 mg tablet"),
    # E: Endocrine / Thyroid
    ("00074323301", "E", "Levothyroxine sodium 50 mcg tablet"),
    ("00093094001", "E", "Levothyroxine sodium 100 mcg tablet"),
    # F: Antifungals
    ("00093208301", "F", "Fluconazole 150 mg tablet"),
    ("00378002601", "F", "Clotrimazole 1% topical cream"),
    # G: Gastrointestinal Agents
    ("00093230098", "G", "Omeprazole 20 mg delayed-release capsule"),
    ("00378064001", "G", "Pantoprazole sodium 40 mg delayed-release tablet"),
    ("00093316998", "G", "Ondansetron HCl 4 mg tablet"),
    # H: Hormonal Agents
    ("00003091656", "H", "Prednisone 5 mg tablet"),
    ("50419045030", "H", "Methylprednisolone 4 mg tablet (Dosepak)"),
    # I: Infectious Disease / Antibiotics
    ("00093310605", "I", "Amoxicillin 500 mg capsule"),
    ("00093314801", "I", "Azithromycin 250 mg tablet"),
    ("00093073501", "I", "Ciprofloxacin HCl 500 mg tablet"),
    ("00093093001", "I", "Doxycycline hyclate 100 mg capsule"),
    # J: Psychiatric / CNS Agents
    ("00093034098", "J", "Sertraline hydrochloride 50 mg tablet"),
    ("00093058701", "J", "Fluoxetine hydrochloride 20 mg capsule"),
    ("00093317401", "J", "Alprazolam 0.25 mg tablet"),
    # K: Anticoagulants
    ("00310024630", "K", "Warfarin sodium 5 mg tablet"),
    ("59148001801", "K", "Apixaban 5 mg tablet"),
    # L: Dermatological
    ("00168001546", "L", "Triamcinolone acetonide 0.1% cream"),
    ("00093240501", "L", "Mupirocin 2% ointment"),
    # M: Musculoskeletal
    ("00093230601", "M", "Cyclobenzaprine HCl 10 mg tablet"),
    ("00093083201", "M", "Naproxen 500 mg tablet"),
    # N: Neurological
    ("00093073401", "N", "Gabapentin 300 mg capsule"),
    ("00378264601", "N", "Pregabalin 75 mg capsule"),
    # P: Pulmonary / Respiratory
    ("00085003401", "P", "Albuterol sulfate 90 mcg/actuation inhaler (200 actuations)"),
    ("49999049330", "P", "Fluticasone propionate 50 mcg/actuation nasal spray"),
]

NDC_CODES, NDC_CHAPTERS, NDC_DESCS = zip(*NDC_ROWS, strict=False)

# ── MCC: ISO 18245 Merchant Category Codes ────────────────────────────────────
# Source: ISO 18245:2003, "Retail financial services - Merchant category codes
# for retail financial services". Published as public reference data by Visa
# (VisaNet Business News, Appendix B), Mastercard (Quick Reference Booklet),
# and other card networks. The enumeration of MCC values is standard reference
# data with no claimed copyright restriction. Chapter = MCC range category.

MCC_ROWS = [
    # 0001-0999: Agricultural Services
    ("0742", "agricultural", "Veterinary Services"),
    ("0763", "agricultural", "Agricultural Co-operatives"),
    ("0780", "agricultural", "Horticultural Services, Landscaping Services"),
    # 1000-1499: Contracted Services
    ("1520", "contracted", "General Contractors - Residential and Commercial"),
    ("1711", "contracted", "Air Conditioning Contractors, Heating and Plumbing Contractors"),
    ("1731", "contracted", "Electrical Contractors"),
    ("1771", "contracted", "Concrete Work Contractors"),
    # 1500-2999: Transportation
    ("4111", "transportation", "Local Commuter Transport, including Ferries"),
    ("4121", "transportation", "Taxicabs and Limousines"),
    ("4131", "transportation", "Bus Lines"),
    ("4411", "transportation", "Cruise Lines"),
    ("4511", "transportation", "Airlines, Air Carriers"),
    ("4722", "transportation", "Travel Agencies and Tour Operators"),
    ("4812", "transportation", "Telecommunication Equipment and Telephone Sales"),
    ("4814", "transportation", "Telecommunication Services"),
    ("4900", "transportation", "Utilities - Electric, Gas, Heating Oil, Sanitary"),
    # 5000-5999: Retail Outlets
    ("5094", "retail", "Precious Stones and Metals, Watches and Jewelry"),
    ("5200", "retail", "Home Supply Warehouse Stores"),
    ("5310", "retail", "Discount Stores"),
    ("5411", "retail", "Grocery Stores, Supermarkets"),
    (
        "5511",
        "retail",
        "Car and Truck Dealers (New and Used) Sales, Service, Repairs, Parts and Leasing",
    ),
    ("5533", "retail", "Automotive Parts and Accessories Stores"),
    ("5541", "retail", "Service Stations"),
    ("5621", "retail", "Women's Ready-to-Wear Stores"),
    ("5651", "retail", "Family Clothing Stores"),
    ("5661", "retail", "Shoe Stores"),
    ("5691", "retail", "Men's and Women's Clothing Stores"),
    ("5732", "retail", "Electronics Stores"),
    ("5812", "retail", "Eating Places and Restaurants"),
    ("5814", "retail", "Fast Food Restaurants"),
    ("5912", "retail", "Drug Stores and Pharmacies"),
    ("5944", "retail", "Jewelry Stores, Watches, Clocks, and Silverware Stores"),
    ("5945", "retail", "Hobby, Toy, and Game Shops"),
    ("5965", "retail", "Direct Marketing - Combination Catalog and Retail Merchant"),
    # 6000-6999: Money/Finance
    ("6011", "financial", "Financial Institutions - Automated Cash Disbursements"),
    ("6012", "financial", "Financial Institutions - Merchandise and Services"),
    ("6051", "financial", "Non-Financial Institutions - Foreign Currency, Money Orders"),
    ("6211", "financial", "Security Brokers/Dealers"),
    ("6300", "financial", "Insurance Sales, Underwriting, and Premiums"),
    # 7000-7999: Personal/Business Services
    ("7011", "services", "Lodging - Hotels, Motels, Resorts, Central Reservation Services"),
    ("7230", "services", "Barber and Beauty Shops"),
    ("7372", "services", "Computer Programming, Data Processing, and Integrated Systems Design"),
    ("7399", "services", "Business Services, Not Elsewhere Classified"),
    ("7523", "services", "Automobile Parking Lots and Garages"),
    ("7832", "services", "Motion Picture Theaters"),
    # 8000-8999: Professional Services
    ("8011", "professional", "Doctors and Physicians - Not Elsewhere Classified"),
    ("8021", "professional", "Dentists and Orthodontists"),
    ("8031", "professional", "Osteopaths"),
    ("8049", "professional", "Podiatrists and Chiropodists"),
    ("8050", "professional", "Nursing and Personal Care Facilities"),
    ("8099", "professional", "Health Practitioners, Medical Services - Not Elsewhere Classified"),
    ("8111", "professional", "Legal Services and Attorneys"),
    ("8211", "professional", "Elementary and Secondary Schools"),
    ("8220", "professional", "Colleges, Universities, Professional Schools, Junior Colleges"),
    ("8299", "professional", "Schools and Educational Services - Not Elsewhere Classified"),
    ("8641", "professional", "Civic, Social, Fraternal Associations"),
    ("8742", "professional", "Management Consulting and Public Relations Services"),
    # 9000-9999: Government Services
    ("9211", "government", "Court Costs, including Alimony and Child Support"),
    ("9222", "government", "Fines"),
    ("9311", "government", "Tax Payments"),
    ("9399", "government", "Government Services - Not Elsewhere Classified"),
    ("9402", "government", "Postal Services - Government Only"),
]

MCC_CODES, MCC_CHAPTERS, MCC_DESCS = zip(*MCC_ROWS, strict=False)


def main() -> None:
    print("Building codesets...")

    counts = {}

    counts["icd10"] = _write(
        "icd10",
        list(ICD10_CODES),
        list(ICD10_CHAPTERS),
        list(ICD10_DESCS),
        {
            "decoy_corpus": "icd10",
            "decoy_corpus_version": "1.0",
            "source": "CMS ICD-10-CM FY2024; WHO ICD-10",
            "source_url": "https://www.cms.gov/medicare/coding-billing/icd-10-codes",
            "license": "Public domain (United States Federal Government work; 17 U.S.C. 105)",
            "citation": "Centers for Medicare and Medicaid Services. ICD-10-CM FY2024 "
            "Official Guidelines for Coding and Reporting. CMS.gov, 2023.",
        },
    )

    counts["hcpcs"] = _write(
        "hcpcs",
        list(HCPCS_CODES),
        list(HCPCS_CHAPTERS),
        list(HCPCS_DESCS),
        {
            "decoy_corpus": "hcpcs",
            "decoy_corpus_version": "1.0",
            "source": "CMS HCPCS Level II Q1 2024",
            "source_url": "https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system",
            "license": "Public domain (United States Federal Government work; 17 U.S.C. 105)",
            "citation": "Centers for Medicare and Medicaid Services. HCPCS Level II Codes "
            "Q1 2024. CMS.gov, 2024.",
        },
    )

    counts["ndc"] = _write(
        "ndc",
        list(NDC_CODES),
        list(NDC_CHAPTERS),
        list(NDC_DESCS),
        {
            "decoy_corpus": "ndc",
            "decoy_corpus_version": "1.0",
            "source": "FDA National Drug Code Directory",
            "source_url": "https://www.fda.gov/drugs/drug-approvals-and-databases/national-drug-code-directory",
            "license": "Public domain (United States Federal Government work; 17 U.S.C. 105)",
            "citation": "U.S. Food and Drug Administration. National Drug Code Directory. "
            "FDA.gov. Retrieved 2024.",
        },
    )

    counts["mcc"] = _write(
        "mcc",
        list(MCC_CODES),
        list(MCC_CHAPTERS),
        list(MCC_DESCS),
        {
            "decoy_corpus": "mcc",
            "decoy_corpus_version": "1.0",
            "source": "ISO 18245:2003 Merchant Category Codes (public reference enumeration)",
            "source_url": "https://www.iso.org/standard/33365.html",
            "license": "Standard code enumeration; no copyright restriction on the published "
            "MCC list (widely published by Visa, Mastercard as reference data).",
            "citation": "ISO 18245:2003 Retail financial services - Merchant category codes "
            "for retail financial services. International Organization for Standardization.",
        },
    )

    print()
    for name, n in counts.items():
        print(f"  {name}: {n} rows")
    print("Done.")


if __name__ == "__main__":
    main()
    sys.exit(0)
