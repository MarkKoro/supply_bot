"""Curated food examples, not prescriptions or quantitative nutrient tables.

NIH ODS vitamin/mineral/omega-3 fact sheets and USDA protein-food guidance.
Composition of the actual product is checked on its label. Diet/group exclusions
filter examples; missing matching examples stay unknown instead of inventing one.
"""
import re

ODS='https://ods.od.nih.gov/factsheets/'
USDA='https://www.myplate.gov/eat-healthy/protein-foods'
REGISTRY={'fiber': {'name': 'Клетчатка', 'unit': 'g'}, 'sugar': {'name': 'Сахара', 'unit': 'g'}, 'saturated_fat': {'name': 'Насыщенные жиры', 'unit': 'g'}, 'monounsaturated_fat': {'name': 'Мононенасыщенные жиры', 'unit': 'g'}, 'polyunsaturated_fat': {'name': 'Полиненасыщенные жиры', 'unit': 'g'}, 'trans_fat': {'name': 'Трансжиры', 'unit': 'g'}, 'omega_3': {'name': 'Омега-3', 'unit': 'g'}, 'omega_6': {'name': 'Омега-6', 'unit': 'g'}, 'epa': {'name': 'ЭПК (омега-3)', 'unit': 'mg'}, 'dha': {'name': 'ДГК (омега-3)', 'unit': 'mg'}, 'ala': {'name': 'АЛК (омега-3)', 'unit': 'g'}, 'cholesterol': {'name': 'Холестерин', 'unit': 'mg'}, 'water': {'name': 'Вода в пище', 'unit': 'g'}, 'alcohol': {'name': 'Алкоголь', 'unit': 'g'}, 'calcium': {'name': 'Кальций', 'unit': 'mg'}, 'iron': {'name': 'Железо', 'unit': 'mg'}, 'magnesium': {'name': 'Магний', 'unit': 'mg'}, 'phosphorus': {'name': 'Фосфор', 'unit': 'mg'}, 'potassium': {'name': 'Калий', 'unit': 'mg'}, 'sodium': {'name': 'Натрий', 'unit': 'mg'}, 'zinc': {'name': 'Цинк', 'unit': 'mg'}, 'copper': {'name': 'Медь', 'unit': 'mg'}, 'manganese': {'name': 'Марганец', 'unit': 'mg'}, 'selenium': {'name': 'Селен', 'unit': 'ug'}, 'iodine': {'name': 'Йод', 'unit': 'ug'}, 'chromium': {'name': 'Хром', 'unit': 'ug'}, 'molybdenum': {'name': 'Молибден', 'unit': 'ug'}, 'chloride': {'name': 'Хлориды', 'unit': 'mg'}, 'vitamin_a_rae': {'name': 'Витамин A (ретиноловый эквивалент)', 'unit': 'ug'}, 'retinol': {'name': 'Ретинол', 'unit': 'ug'}, 'beta_carotene': {'name': 'Бета-каротин', 'unit': 'ug'}, 'vitamin_b1': {'name': 'Витамин B1', 'unit': 'mg'}, 'vitamin_b2': {'name': 'Витамин B2', 'unit': 'mg'}, 'vitamin_b3': {'name': 'Ниацин', 'unit': 'mg'}, 'vitamin_b5': {'name': 'Пантотеновая кислота', 'unit': 'mg'}, 'vitamin_b6': {'name': 'Витамин B6', 'unit': 'mg'}, 'biotin': {'name': 'Биотин', 'unit': 'ug'}, 'folate_dfe': {'name': 'Фолат (пищевой эквивалент)', 'unit': 'ug'}, 'folic_acid': {'name': 'Фолиевая кислота', 'unit': 'ug'}, 'vitamin_b12': {'name': 'Витамин B12', 'unit': 'ug'}, 'vitamin_c': {'name': 'Витамин C', 'unit': 'mg'}, 'vitamin_d': {'name': 'Витамин D', 'unit': 'ug'}, 'vitamin_e': {'name': 'Витамин E', 'unit': 'mg'}, 'vitamin_k': {'name': 'Витамин K', 'unit': 'ug'}, 'choline': {'name': 'Холин', 'unit': 'mg'}, 'leucine': {'name': 'Лейцин', 'unit': 'g'}, 'isoleucine': {'name': 'Изолейцин', 'unit': 'g'}, 'valine': {'name': 'Валин', 'unit': 'g'}, 'lysine': {'name': 'Лизин', 'unit': 'g'}, 'methionine': {'name': 'Метионин', 'unit': 'g'}, 'phenylalanine': {'name': 'Фенилаланин', 'unit': 'g'}, 'threonine': {'name': 'Треонин', 'unit': 'g'}, 'tryptophan': {'name': 'Триптофан', 'unit': 'g'}, 'histidine': {'name': 'Гистидин', 'unit': 'g'}}

FOODS={
 'beans':('Фасоль, чечевица или нут',(),False),
 'tofu':('Тофу или темпе',('soy',),False),
 'nuts':('Орехи',('nuts',),False),
 'seeds':('Семена подсолнечника или тыквы',(),False),
 'flax':('Молотое льняное семя или чиа — источник АЛК (омега-3)',(),False),
 'walnuts':('Грецкие орехи — источник АЛК (омега-3)',('nuts',),False),
 'grains':('Овсяная или гречневая крупа',('gluten',),False),
 'rice':('Рис или картофель',(),False),
 'wholebread':('Цельнозерновой хлеб',('gluten',),False),
 'greens':('Зелёные листовые овощи',(),False),
 'broccoli':('Брокколи',(),False),
 'carrots':('Морковь или батат',(),False),
 'peppers':('Сладкий перец, киви или цитрусовые',(),False),
 'fruit':('Фрукты и ягоды',(),False),
 'mushrooms':('Грибы',(),False),
 'avocado':('Авокадо',(),False),
 'olive':('Оливковое масло',(),False),
 'sunflower':('Подсолнечное масло',(),False),
 'eggs':('Приготовленные яйца',('eggs',),True),
 'dairy':('Молоко, йогурт или творог',('dairy',),True),
 'fish':('Лосось, сардины или сельдь',('fish',),True),
 'whitefish':('Морская рыба',('fish',),True),
 'poultry':('Курица или индейка',('poultry',),True),
 'meat':('Говядина',('meat',),True),
 'shellfish':('Моллюски',('shellfish',),True),
 'fortb12':('Продукт, обогащённый B12: напиток, хлопья или пищевые дрожжи; проверьте B12 на этикетке',(),False),
 'fortd':('Напиток или хлопья, обогащённые витамином D; проверьте этикетку',(),False),
 'fortcalcium':('Напиток, обогащённый кальцием; проверьте состав и этикетку',(),False),
 'fortfolic':('Хлопья или хлеб с добавленной фолиевой кислотой; проверьте этикетку',('gluten',),False),
 'water':('Питьевая вода; вода также содержится в еде',(),False),
 'label':('Проверьте состав конкретного товара на этикетке',(),False)}

SOURCES={
 'fiber':['beans','grains','fruit','greens'], 'sugar':['fruit','dairy','label'],
 'saturated_fat':['dairy','eggs','label'], 'monounsaturated_fat':['olive','avocado','nuts'],
 'polyunsaturated_fat':['seeds','nuts','sunflower'], 'trans_fat':['label'],
 'omega_3':['fish','flax','walnuts'],'omega_6':['sunflower','nuts','seeds'],
 'epa':['fish'],'dha':['fish'],'ala':['flax','walnuts'],
 'cholesterol':['eggs','dairy','shellfish'],'water':['water','fruit'],'alcohol':['label'],
 'calcium':['dairy','fortcalcium','tofu'],'iron':['beans','meat','seeds'],
 'magnesium':['beans','nuts','seeds','grains'],'phosphorus':['dairy','beans','poultry'],
 'potassium':['beans','rice','avocado','fruit'],'sodium':['label'],'zinc':['meat','beans','nuts'],
 'copper':['shellfish','nuts','seeds'],'manganese':['grains','nuts','beans'],
 'selenium':['fish','eggs','poultry'],'iodine':['whitefish','dairy','eggs'],
 'chromium':['wholebread','meat','fruit'],'molybdenum':['beans','grains','nuts'],'chloride':['label'],
 'vitamin_a_rae':['carrots','greens','eggs'],'retinol':['eggs','dairy'],
 'beta_carotene':['carrots','greens'],'vitamin_b1':['grains','beans','seeds'],
 'vitamin_b2':['dairy','eggs','mushrooms'],'vitamin_b3':['poultry','fish','nuts'],
 'vitamin_b5':['mushrooms','avocado','poultry'],'vitamin_b6':['beans','poultry','rice'],
 'biotin':['eggs','fish','seeds'],'folate_dfe':['greens','beans','broccoli'],
 'folic_acid':['fortfolic'],'vitamin_b12':['fish','eggs','dairy','fortb12'],
 'vitamin_c':['peppers','broccoli'],'vitamin_d':['fish','fortd'],
 'vitamin_e':['seeds','nuts','sunflower'],'vitamin_k':['greens','broccoli'],
 'choline':['eggs','beans','poultry']}
for _code in ('leucine','isoleucine','valine','lysine','methionine','phenylalanine','threonine','tryptophan','histidine'):
    SOURCES[_code]=['beans','tofu','eggs','fish','poultry']

FACTS={
 'calcium':'Calcium','iron':'Iron','magnesium':'Magnesium','phosphorus':'Phosphorus',
 'potassium':'Potassium','zinc':'Zinc','copper':'Copper','manganese':'Manganese',
 'selenium':'Selenium','iodine':'Iodine','chromium':'Chromium','molybdenum':'Molybdenum',
 'vitamin_a_rae':'VitaminA','retinol':'VitaminA','beta_carotene':'VitaminA',
 'vitamin_b1':'Thiamin','vitamin_b2':'Riboflavin','vitamin_b3':'Niacin','vitamin_b5':'PantothenicAcid',
 'vitamin_b6':'VitaminB6','biotin':'Biotin','folate_dfe':'Folate','folic_acid':'Folate',
 'vitamin_b12':'VitaminB12','vitamin_c':'VitaminC','vitamin_d':'VitaminD','vitamin_e':'VitaminE',
 'vitamin_k':'VitaminK','choline':'Choline','omega_3':'Omega3FattyAcids','ala':'Omega3FattyAcids',
 'epa':'Omega3FattyAcids','dha':'Omega3FattyAcids'}

def examples(code,preferences=None):
    if code not in REGISTRY:raise ValueError('unknown_nutrient')
    p=preferences or {};user_exclusions=set(p.get('excluded_food_groups') or []);excluded=set(user_exclusions)
    if p.get('diet')=='vegetarian':excluded.update(('fish','shellfish','meat','poultry'))
    if p.get('diet')=='vegan':excluded.update(('fish','shellfish','meat','poultry','eggs','dairy'))
    items=[]
    for ident in SOURCES[code]:
        name,groups,animal=FOODS[ident]
        if excluded.intersection(groups) or animal and p.get('diet')=='vegan':continue
        if ident.startswith('fort') and user_exclusions:continue  # Brand composition is unknown.
        items.append({'name':name,'groups':list(groups),'example_id':ident})
    note='Примеры пищевых источников. Состав и порция влияют на количество; здесь нет назначения добавки или дозы.'
    if code in ('trans_fat','alcohol','sodium','chloride','sugar','saturated_fat','cholesterol'):
        note='Справка о составе, а не предложение увеличивать употребление этого вещества.'
    if code=='vitamin_b12':note+=' Обычные необогащённые растительные продукты не считаем надёжным источником B12.'
    if code in ('epa','dha'):note+=' АЛК из льна и орехов не заменяет ЭПК и ДГК; веганский продукт проверяйте по этикетке.'
    if code=='iodine':note+=' Содержание йода в водорослях очень различается; универсальную порцию не предлагаем.'
    if not items:note+=' Под ваши исключения подтверждённых примеров пока нет; новый источник не выдумываем.'
    return {'code':code,'name':REGISTRY[code]['name'],'items':items,'note':note,
        'source_url':ODS+FACTS[code]+'-HealthProfessional/' if code in FACTS else USDA,
        'version':1,'amounts':None}

def matches(text):
    normalized=re.sub(r'[^\w]','',str(text or '').lower())
    aliases={'b12':'vitamin_b12','в12':'vitamin_b12','витаминb12':'vitamin_b12','витаминв12':'vitamin_b12',
        'omega3':'omega_3','омега3':'omega_3','b1':'vitamin_b1','b2':'vitamin_b2','b3':'vitamin_b3',
        'b5':'vitamin_b5','b6':'vitamin_b6','железо':'iron','магний':'magnesium','цинк':'zinc'}
    if normalized in aliases:return [aliases[normalized]]
    return [c for c,v in REGISTRY.items() if normalized in (re.sub(r'[^\w]','',c.lower()),re.sub(r'[^\w]','',v['name'].lower()))]

def macro_examples(kind,preferences=None,country=None):
    p=preferences or {};excluded=set(p.get('excluded_food_groups') or [])
    if p.get('diet') in ('vegetarian','vegan'):excluded.update(('fish','shellfish','meat','poultry'))
    if p.get('diet')=='vegan':excluded.update(('eggs','dairy'))
    ids={'protein':['beans','tofu','eggs','dairy','poultry','fish'],
         'fat':['olive','seeds','nuts'],'carbs':['rice','grains','fruit','wholebread']}[kind]
    if country in ('TH','VN','JP','CN','KR') and kind=='protein':ids=['tofu','beans','eggs','fish','poultry','dairy']
    return [FOODS[i][0] for i in ids if not excluded.intersection(FOODS[i][1])][:3]
