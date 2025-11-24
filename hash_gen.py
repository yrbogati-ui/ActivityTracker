import bcrypt
print(bcrypt.hashpw("1234567890".encode(), bcrypt.gensalt()).decode())
