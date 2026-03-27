import pdfplumber
import sys

def debug_pdf(sciezka_pdf):
    try:
        with pdfplumber.open(sciezka_pdf) as pdf:
            strona = pdf.pages[0]
            # Wyciągamy czysty tekst zamiast tabeli, żeby zobaczyć kolejność linii
            tekst = strona.extract_text()
            print("--- PEŁNA TREŚĆ TEKSTOWA PDF ---")
            print(tekst)
            print("\n--- STRUKTURA TABELI (DEBUG) ---")
            tabela = strona.extract_table()
            for i, row in enumerate(tabela):
                print(f"Wiersz {i}: {row}")
    except Exception as e:
        print(f"Błąd: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        debug_pdf(sys.argv[1])