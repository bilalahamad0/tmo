"""
Read pdf file
parse data for each numbers --> Total
Map with name
generate summarized bill

"""

import math
from PyPDF2 import PdfReader

filename = "SummaryBillJan2024.pdf"
filepath = "/Users/bilalmac2/Downloads/"

file = filepath + filename

reader = PdfReader(file)
number_of_pages = len(reader.pages)
page = reader.pages[1]
text = page.extract_text().split('\n')

# print(text)
print('#'*21)
month_name = text[1].split(',')[0] + "," + text[1].split(',')[1][:5]
if 'Totals' in text[6]:
    total_bill = text[6].split()[-1]
    print(f'{month_name}: {total_bill}')

if 'Account' in text[7]:
    base_charge = float(text[7].split()[-1][1:])
    # print(base_charge)

counter = 0
final_dict = dict()
for _ in text:
    if '(' in _:
        temp_ = _.split()
        phone_number = temp_[0] + temp_[1]
        bill = float(temp_[-1][1:])
        final_dict[phone_number] = bill
        # print(phone_number, bill)
        counter += 1
    if counter > 6:
        break

# print(final_dict)

name_mapping = {'(847)443-5295': 'Bilal',
                '(703)479-8351': 'Bilal2',
                '(650)797-3800': 'Karan',
                '(408)677-1812': 'Mudit',
                '(408)784-6924': 'Utsav',
                '(408)896-8130': 'Sachin',
                '(408)898-8413': 'Sambit'
                }

members = len(final_dict.keys())
member_base_charge = (base_charge + 10.0)/members
print('#'*21)
total_check = 0
ba_im = 0
send_to_sachin = 0
for k, v in enumerate(final_dict):
    bill = final_dict[v]
    # print(k, v)
    if name_mapping[v] == 'Bilal2':
        bill -= 10.0
    bill += member_base_charge
    # print(round(bill, 2))
    total_check += bill
    if name_mapping[v] == 'Bilal' or name_mapping[v] == 'Karan' or name_mapping[v] == 'Bilal2':
        send_to_sachin += bill
        if name_mapping[v] == 'Karan':
            print(f'{name_mapping[v]}: {round(bill, 2)}')
            continue
        ba_im += bill
    print(f'{name_mapping[v]}: {round(bill, 2)}')
print('#'*21)
print(f'Total Bill: {round(total_check, 2)}')
print('#'*21)
print(f"Bilal+Bilal2: ${round(ba_im, 2)}")
print(f"Send to Sachin: ${round(send_to_sachin, 2)}")
