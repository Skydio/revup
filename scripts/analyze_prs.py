#!/usr/bin/env python3
import argparse
import datetime
import json

import dateutil.parser
import dateutil.relativedelta

# A script for analyzing the revup usage within a particular repo. To use, first query github
# with the command
# gh pr list --state merged --json author --json headRefName --json mergedAt --json number --limit 20000 > pr_list.json
# (set the limit as needed to be greater than the total prs in your repo)
# Running this script will show you how many prs out of the total were made with revup, and will
# show the top contributors by pr count.

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--filename", type=str, help="Filename of json file to analyze", required=True
    )
    parser.add_argument(
        "--limit_date", action="store_true", help="Whether to limit analysis by date"
    )
    parser.add_argument(
        "--date-months",
        type=int,
        default=3,
        help="If limit_date=True, number of months to limit to",
    )
    parser.add_argument(
        "--num-users", type=int, default=15, help="Number of users to list in the ranking"
    )
    parser.add_argument(
        "--sort-by-revup",
        action="store_true",
        help="Whether to sort by number of revup prs or by all prs",
    )
    args = parser.parse_args()

    text = open(args.filename).read()

    all_prs = json.loads(text)

    users = {}

    total = 0
    total_revup = 0

    for pr in all_prs:
        name = pr["author"]["login"]
        if name not in users:
            users[name] = [0, 0]

        is_revup = "/revup/" in pr["headRefName"]

        months_to_search = args.date_months
        start_date = datetime.datetime.now() + dateutil.relativedelta.relativedelta(
            months=-months_to_search
        )

        dateat = dateutil.parser.parse(pr["mergedAt"])
        date_in_range = start_date.timestamp() <= dateat.timestamp()

        if args.limit_date and not date_in_range:
            continue

        total += 1
        users[name][0] += 1
        if is_revup:
            total_revup += 1
            users[name][1] += 1

    print("Total PRs: {}".format(total))
    print("Total revup PRs: {}".format(total_revup))
    print(
        "Top {} contributors by number of {}prs".format(
            args.num_users, "revup " if args.sort_by_revup else ""
        )
    )

    users_sorted = []
    for user in users:
        users_sorted.append((user, users[user][0], users[user][1]))

    users_sorted.sort(key=lambda tup: tup[1 + args.sort_by_revup], reverse=True)

    for i in range(args.num_users):
        print(
            "{}: {} with {} PRs and {} revup PRs".format(
                i + 1, users_sorted[i][0], users_sorted[i][1], users_sorted[i][2]
            )
        )
