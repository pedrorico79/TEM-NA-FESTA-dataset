# Tendências dos produtos ativos do sistema no Google Trends.
# A lógica fica em tnf_tendencias.py, que também serve de handler da Lambda do
# ambiente na infraestrutura. Uso: python ./script/script-tendencias-produtos-TNF.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tnf_tendencias import main  # noqa: E402

if __name__ == "__main__":
    main()
