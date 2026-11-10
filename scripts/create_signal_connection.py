"""Issue a credential for one linked Signal account. Run only on the app host."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.providers.signal.sender import connection_token

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('number', help='Linked Signal account in international format (+country code and number)')
    args = parser.parse_args()
    print(connection_token(args.number))
