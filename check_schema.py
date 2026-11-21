from db.connection import get_connection

con = get_connection()
cur = con.cursor()
cur.execute("SHOW COLUMNS FROM stock_serial")
for row in cur.fetchall():
    print(row)
print("---")
cur.execute("SHOW COLUMNS FROM stock_non_serial")
for row in cur.fetchall():
    print(row)
con.close()