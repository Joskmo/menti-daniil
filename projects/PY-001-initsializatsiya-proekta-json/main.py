import json

with open(
    r"C:/Users/Даниил/Desktop/proga/py/json parse practice/test.json",
    encoding="utf-8",
) as file:
    data = json.load(file)

last_id = data[-1]['id']
nextid = last_id + 1

name = str(input("Введите ваше имя: "))
last_name = str(input("Введите вашу фамилию: "))

print("Добавление данных!")

new_User = {
    "id": nextid,
    "first_name": name,
    "last_name": last_name
}

data.append(new_User)

with open(
    r"C:/Users/Даниил/Desktop/proga/py/json parse practice/test.json",
    "w",
    encoding="utf-8",
) as file:
    json.dump(data, file, ensure_ascii=False, indent=2)

print(f"Данные успешно добавлены! Ваш id: {nextid}")
