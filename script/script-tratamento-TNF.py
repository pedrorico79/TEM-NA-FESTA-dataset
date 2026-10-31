# Base da "Especificação de Base de Dados": POF/IBGE + Google Trends (mês a mês).
# A lógica fica em tnf_pof_trends.py, que também serve de handler da Lambda
# semanal da infraestrutura. Uso: python ./script/script-tratamento-TNF.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tnf_pof_trends import main  # noqa: E402

if __name__ == "__main__":
    main()
