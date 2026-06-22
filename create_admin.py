#!/usr/bin/env python3
"""
Crée ou réinitialise le compte administrateur local.
Usage : python3 create_admin.py [--callsign F8AOF] [--password monmdp]

Par défaut : indicatif=ADMIN  mot de passe=admin
"""
import sys, hashlib, sqlite3, argparse
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "nfm.db"

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--callsign", default="ADMIN",
                        help="Indicatif admin (defaut: ADMIN)")
    parser.add_argument("--password", default="admin",
                        help="Mot de passe (defaut: admin)")
    parser.add_argument("--email", default="admin@nfm.local",
                        help="Email (defaut: admin@nfm.local)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERREUR : base de données introuvable : {DB_PATH}")
        print("Lancez d'abord ./nfm.sh pour initialiser la base.")
        sys.exit(1)

    callsign = args.callsign.upper()
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    existing = db.execute(
        "SELECT id, callsign FROM users WHERE callsign=?",
        (callsign,)).fetchone()

    if existing:
        db.execute(
            "UPDATE users SET password_hash=?, confirmed=1, is_admin=1, "
            "email=? WHERE callsign=?",
            (hash_password(args.password), args.email, callsign))
        db.commit()
        print(f"\n  OK Compte mis a jour :")
    else:
        db.execute(
            "INSERT INTO users (callsign, email, password_hash, confirmed, is_admin) "
            "VALUES (?, ?, ?, 1, 1)",
            (callsign, args.email, hash_password(args.password)))
        db.commit()
        print(f"\n  OK Compte cree :")

    db.close()
    print(f"  Indicatif  : {callsign}")
    print(f"  Mot de passe : {args.password}")
    print(f"  Email      : {args.email}")
    print(f"  Admin      : oui")
    print(f"\n  Connexion  : http://localhost:5000/login\n")

if __name__ == "__main__":
    main()
