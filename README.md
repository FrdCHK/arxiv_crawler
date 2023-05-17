# Arxiv_weekly
A little crawler which can help you to get your own paper list from https://arxiv.org/ every week.

The account information should be put in account.json (create by your own first):

    {
      "sender": {
        "server": "smtp server",
        "port": 994,
        "user": "email address",
        "passwd": "password"
      },
      "receiver": "email address"
    }

The keywords should be put in keywords.json, an example is available:

    {
      "title": [
        ["A"],
        ["B"]
      ],
      "subject": [
        ["C", "D"],
        ["E", "F"]
      ],
      "author": [
        ["G"]
      ]
    }

in which, the logical operation is: (A|B)|((C&D)|(E&F))|G
