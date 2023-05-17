# -*- coding: utf-8 -*-
"""
Created on Sat Jul 14 14:24:58 2018

@author: ZZH, edited by Jingdong Zhang
"""
import json
import numpy as np
import requests
import time
import pandas as pd
from bs4 import BeautifulSoup
import random
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header


def get_one_page(url):
    response = requests.get(url)
    print(response.status_code)
    while response.status_code == 403:
        time.sleep(500 + random.uniform(0, 500))
        response = requests.get(url)
        print(response.status_code)
    print(response.status_code)
    if response.status_code == 200:
        return response.text

    return None


def main(keywords, sender, receiver):
    url = 'https://arxiv.org/list/astro-ph/pastweek?show=1000'
    html = get_one_page(url)
    soup = BeautifulSoup(html, features='html.parser')
    # content = soup.dl
    contents = soup.find_all('dl')
    date = soup.find_all('h3')
    col_name = ['date', 'id', 'title', 'authors', 'subjects', 'subject_split']
    papers_week = pd.DataFrame(columns=col_name)
    for i in range(len(contents)):
        list_ids = contents[i].find_all('a', title='Abstract')
        list_title = contents[i].find_all('div', class_='list-title mathjax')
        list_authors = contents[i].find_all('div', class_='list-authors')
        list_subjects = contents[i].find_all('div', class_='list-subjects')
        list_subject_split = []
        for subjects in list_subjects:
            subjects = subjects.text.split(': ', maxsplit=1)[1]
            subjects = subjects.replace('\n\n', '')
            subjects = subjects.replace('\n', '')
            subject_split = subjects.split('; ')
            list_subject_split.append(subject_split)

        items = []
        for j, paper in enumerate(zip(list_ids, list_title, list_authors, list_subjects, list_subject_split)):
            items.append([date[i].text, paper[0].text, paper[1].text, paper[2].text, paper[3].text, paper[4]])
        papers_day = pd.DataFrame(columns=col_name, data=items)
        papers_week = pd.concat([papers_week, papers_day], ignore_index=True)
    print('get paper success')

    '''key_word selection'''
    paper_select_index = np.full(papers_week.index.size, False)
    for title_kw_list in keywords["title"]:
        title_select_index = np.full(papers_week.index.size, True)
        for item in title_kw_list:
            title_select_index = title_select_index & papers_week['title'].str.contains(item, case=False)
        paper_select_index = paper_select_index | title_select_index
    for subject_list in keywords["subject"]:
        subject_select_index = np.full(papers_week.index.size, True)
        for item in subject_list:
            subject_select_index = subject_select_index & papers_week['subjects'].str.contains(item, case=False)
        paper_select_index = paper_select_index | subject_select_index
    for author_list in keywords["author"]:
        author_select_index = np.full(papers_week.index.size, True)
        for item in author_list:
            author_select_index = author_select_index & papers_week['subjects'].str.contains(item, case=False)
        paper_select_index = paper_select_index | author_select_index
    selected_papers = papers_week[paper_select_index].copy()
    selected_papers.reset_index(drop=True, inplace=True)
    print('keyword selection success')

    selected_papers["date"] = selected_papers["date"].apply(lambda s: s.replace('<h3>', ''))
    selected_papers["date"] = selected_papers["date"].apply(lambda s: s.replace('</h3>', ''))
    selected_papers["datetime"] = selected_papers["date"].apply(lambda s: datetime.strptime(s, '%a, %d %b %Y'))
    selected_papers["id"] = selected_papers["id"].apply(lambda s: s.replace('arXiv:', ''))
    selected_papers["title"] = selected_papers["title"].apply(lambda s: s.replace('\n', ''))
    selected_papers["title"] = selected_papers["title"].apply(lambda s: s.replace('Title: ', ''))
    selected_papers["authors"] = selected_papers["authors"].apply(lambda s: s.replace('Authors:', ''))
    selected_papers["authors"] = selected_papers["authors"].apply(lambda s: s.replace('\n', ''))
    selected_papers["author_split"] = selected_papers["authors"].apply(lambda s: s.split(', '))

    '''send email'''
    msg = "<h2>arXiv this week</h2>"
    papers_gr = selected_papers.groupby('datetime', sort=False)
    for date, gr in papers_gr:
        msg += f"<h3>{date.year}-{date.month}-{date.day}</h3>\n<ol>\n"
        for i, item in gr.iterrows():
            msg += f'<li><b>Title:</b> <a href="https://arxiv.org/abs/{item["id"]}">{item["title"]}</a><br/>' \
                   f'<b>Authors:</b> '
            for count, auth in enumerate(item['author_split']):
                if count:
                    msg += ', '
                msg += f"{auth}"
            msg += "<br/><b>Subjects:</b> "
            for count, subj in enumerate(item['subject_split']):
                if count:
                    msg += ', '
                msg += f"{subj}"
            msg += "</li>\n"
        msg += "</ol>"

    multi_part = MIMEMultipart('alternative')
    multi_part.attach(MIMEText(msg, 'html', 'utf-8'))
    multi_part['From'] = sender['user']
    multi_part['To'] = receiver
    multi_part['Subject'] = Header('arXiv this week', 'utf-8')

    try:
        smtp = smtplib.SMTP_SSL(host=sender['server'], port=sender['port'])
        smtp.login(sender['user'], sender['passwd'])
        smtp.sendmail(sender['user'], receiver, multi_part.as_string())
        smtp.quit()
        print('send email success')
    except smtplib.SMTPException:
        print("error: email not sent!")

    print('finished!')


if __name__ == '__main__':
    with open('keywords.json', 'r') as kwf:
        kws = json.load(kwf)
        with open('account.json', 'r') as accf:
            acc = json.load(accf)
            main(kws, acc["sender"],
                 acc["receiver"])
    time.sleep(1)
