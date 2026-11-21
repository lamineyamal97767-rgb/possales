import pymysql

conn = pymysql.connect(host="127.0.0.1", database="print_db", user="root", password="", charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()


def show(title, sql, args=None):
    print("=== " + title)
    cur.execute(sql, args or ())
    for r in cur.fetchall():
        print(r)


show("sales constraints", "SELECT DISTINCT INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales' ORDER BY INDEX_NAME, SEQ_IN_INDEX")
show("sales_details constraints", "SELECT DISTINCT INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sales_details' AND NON_UNIQUE = 0 ORDER BY INDEX_NAME, SEQ_IN_COLUMN")
show("customer_gifts table", "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'customer_gifts'")
conn.close()
