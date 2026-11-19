# Alias de compatibilidade: a coleta atual usa apenas produtos ativos do sistema.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tnf_tendencias import main  # noqa: E402

if __name__ == "__main__":
    main()
