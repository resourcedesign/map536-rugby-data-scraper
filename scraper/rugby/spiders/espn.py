# -*- coding: utf-8 -*-

import regex
from pprint import pprint

from urllib.parse import urljoin
from collections import defaultdict, OrderedDict

from scrapy import Request
from scrapy.spiders import Spider
from scrapy.exceptions import CloseSpider

from rugby.items import Match, MatchStats, Team, Player, PlayerStats, GameEvent, MatchExtraStats, PlayerExtraStats, Venue
from rugby.loaders import MatchLoader, MatchStatsLoader, TeamLoader, PlayerLoader, PlayerStatsLoader, GameEventLoader, MatchExtraStatsLoader, PlayerExtraStatsLoader, VenueLoader

class ESPN(Spider):
    """Main spider of the scraper, targeting http://stats.espnscrum.com"""

    # Scrapy params
    name = "espn"
    allowed_domains = ["stats.espnscrum.com", "espn.co.uk"]

    # Custom params
    follow_pages = True
    categories = [1, 3]
    start_domain = "http://stats.espnscrum.com/"
    search_path = "/statsguru/rugby/stats/index.html"

    player_params = {
        "class": 1, # ?,
        "template": "results",
        "type": "player",
        "view": "match",
    }

    def _generate_query_params(self, home_or_away = 1, page = 1):
        search_params = OrderedDict([
            ("class", 1), # ?,
            ("home_or_away", home_or_away), # Only returns home team entries
            ("orderby", "date"),
            ("orderbyad", "reverse"),
            ("page", page),
            ("size", 100), # Results per page
            ("spanmin1", "24+Jul+1992"), # Lower bound date
            ("spanval1", "span"), # ?
            ("template", "results"),
            ("type", "team"),
            ("view", "match"),
        ])
        return search_params

    def _generate_query_string(self, query_params):
        sep = ";"
        key_values = ["{}={}".format(k, v) for k, v in query_params.items()]
        return sep.join(key_values)

    def _generate_url(self, domain, path, query_params):
        query_string = self._generate_query_string(query_params)
        return urljoin(domain, "{}?{}".format(path, query_string))

    def _generate_search_url(self, **params):
        query_params = self._generate_query_params(**params)
        return self._generate_url(domain = self.start_domain, path = self.search_path, query_params = query_params)

    def _generate_search_request(self, **params):
        return Request(
            url = self._generate_search_url(**params),
            callback = self.match_list_parse,
            meta = params)

    def start_requests(self):
        """ Method that initializes the spider by getting the first page of the following queries :
        - all matches from all teams
        - from the 24 of july 1992 (date of the change in the way to count points in rugby)
        - ordered by date
        - grouped by home or away
        """
        # Go !
        page = 1
        for category in self.categories:
            self.logger.info("Scraping page {} - {} matches".format(page, "Home" if category == 1 else "Neutral"))
            yield self._generate_search_request(page = page, home_or_away = category)

    def match_list_parse(self, response):
        """ Callback that handles the parsing and processing of the match list table.
        Returns : Match() item, MatchStats() item, Team() item
        """

        # Check if there are matches left to parse on the page.
        rows = response.css("tr.data1")
        if len(rows) == 1:
            msg = rows[0].css("td b::text").extract_first()
            if msg and "No records" in msg.strip():
                self.logger.info("Finished scraping for category \"{}\" !".format("Home" if response.meta["home_or_away"] == 1 else "Neutral"))
                self.categories.remove(int(response.meta["home_or_away"]))

        # If we've finished the scraping for all categories, we can close the spider
        if len(self.categories) == 0:
            self.logger.info("All categories have been scraped ! Closing spider ...")
            raise CloseSpider

        id_fields = {
            'id': 'li:nth-child(6) > a::attr(href)',
            'home_team_id': 'li:nth-child(3) > a::attr(href)',
            'away_team_id': 'li:nth-child(4) > a::attr(href)',
            'ground_id': 'li:nth-child(5) > a::attr(href)'
        }

        meta_fields = {
            "won": "td:nth-child(2)::text",
            "date": "td:nth-child(13) b::text"
        }

        # Variable storing the index offset between the side menu divs and the rows
        offset = None

        for index, links in enumerate(response.css(".engine-dd")):
            if links.css("[id^=\"engine-dd-\"]"):
                # Skip the UI divs
                continue

            if not offset:
                offset = index - 1

            # 1) Extract the basic match info into the Match structure
            loader = MatchLoader(item = Match(), response = response)
            # Subloader that handles the links in the side menu divs
            link_block_loader = loader.nested_css("#engine-dd{}".format(index - offset))
            for field, selector in id_fields.items():
                link_block_loader.add_css(field, selector, re = "\/([0-9]+)\.")
            # Subloader that handles the match info in the table rows (won, date)
            table_row_loader = loader.nested_css("tr.data1:nth-child({})".format(index - offset))
            for field, selector in meta_fields.items():
                table_row_loader.add_css(field, selector)
            # Computed values
            loader.add_value("match_type", response.meta["home_or_away"])
            # Fetch the data
            match = loader.load_item()

            if any(k not in match.keys() for k in ["id", "home_team_id", "away_team_id", "match_type", "won", "date"]):
                # Better safe than sorry
                self.logger.error("Missing IDs for match. Skipping ...")
                continue
            self.logger.info("Found match ! ID : {}".format(match["id"]))

            yield response.follow(
                url = "/statsguru/rugby/match/{}.html".format(match["id"]),
                callback = self.match_page_parse,
                meta = { "match" : match }
            )

        # Get next page link and follow it if there is still data to process
        if self.follow_pages:
            category = int(response.meta["home_or_away"])
            if category in self.categories:
                page = int(response.meta["page"]) + 1
                self.logger.info("Scraping page {} - {} matches".format(page, "Home" if category == 1 else "Neutral"))
                yield self._generate_search_request(page = page, home_or_away=category)

    def player_info_parse(self, response):
        """ Callback that handles the parsing of the player info page (followed by the match iframe callback)
        Returns a populated Player() item
        """

        fields = {
            "Full name": "full_name",
            "Born": "birthday",
            "Height": "height",
            "Weight": "weight"
        }

        infos = response.css("#scrumPlayerContent table .scrumPlayerDesc")
        if infos:
            loader = PlayerLoader(item = response.meta["player_info"], response = response)
            for info in infos:
                title = info.xpath("b/text()").extract_first()
                if title and title in fields.keys():
                    value = info.xpath("text()").extract_first()
                    if value:
                        loader.add_value(fields.get(title), value.strip())
            yield loader.load_item()


    def match_page_parse(self, response):
        """ Callback that acts as a buffer between the match links followed by the match list parser and
        the real processing. Checks that data is available in iframe.
        """
        # Extract iframe url with match data
        iframe = response.css("#win_old::attr(src)").extract_first()

        if iframe:
            yield response.follow(
                url = iframe,
                callback = self._match_iframe_parse,
                meta = response.meta
            )

    def _get_player_id_from_name(self, name, team_dic) :
        """ Method that allows to get the id of a player from his name and the dic of his team
        should accept names as : name, initials name """
        potential = []
        final = []
        name = name.upper().strip()
        for player_id , player_info in team_dic.items() :
            player_name = player_info[0].upper().strip()
            player_name_list = player_name.split(" ")
            researched_name_list = name.split(" ")
            if researched_name_list[-1] in player_name_list[-1] :
                potential.append(player_id)
        if len(potential) == 0 :
            raise RuntimeError("no name was detected")
        elif len(potential) == 1 :
            return potential[0]
        else:
            #if all the potentials are equals :
            if all(potential[0] == rest for rest in potential) :
                return potential[0]
            if len(name.split(" ")) == 1 :
                raise RuntimeError("two many names containing the exact researched name")
            else:
                researched_first_leter = name.split(" ")[0][0]
                researched_last_name = name.split(" ")[-1]
                for potential_id in potential :
                    potential_name = team_dic[potential_id][0].upper().strip()
                    potential_first_letter = potential_name.split(" ")[0][0]
                    potential_last_name = potential_name.split(" ")[-1]
                    if potential_first_letter == researched_first_leter and potential_last_name == researched_last_name:
                        final.append(potential_id)
                if len(final) == 1:
                    return final[0]
                else:
                    #if all final are equals we return it :
                    if final and all(final[0] == rest for rest in final) :
                        return final[0]
                    raise RuntimeError ("could not find name")


    def _parse_match_stats(self, tab, match) :
        """ Parser that handles the content of the per-team "Stats" tab.
        Returns the statistic value for each team for each stat. Generator function"""

        stats = tab.css("table tr")
        if not stats:
            self.logger.error("[{}] No data in \"Match stats\" tab, aborting.".format(match["id"]))
            return

        for stat in stats:
            title = stat.css("td:nth-child(2)::text").extract_first()
            if not title:
                continue
            values = [stat.css("td:nth-child({})::text".format(i)).extract_first() for i in [1, 3]]
            ids = [match["home_team_id"], match["away_team_id"]]
            if not all(values):
                continue
            result = defaultdict(dict)

            for team_id, value in zip(ids, values):
                # Analysing the data itself
                if title == "Penalty goals":
                    cons_attempt_re = regex.match("[0-9]+ from ([0-9]+)", value)
                    if not cons_attempt_re:
                        continue
                    result["pens_attempt"][team_id] = int(cons_attempt_re.captures(1)[0])
                # Attempted drops
                if title == "Dropped goals":
                    drops_re = regex.match("([0-9]+)( \(([0-9]+) missed\))?", value)
                    if not drops_re:
                        continue
                    drops_scored = int(drops_re.captures(1)[0])
                    drops_missed = drops_re.captures(3)
                    if not drops_missed:
                        drops_missed = 0
                    else:
                        drops_missed = int(drops_missed[0])
                    drops_attempt = drops_scored + drops_missed
                    result["drops_attempt"][team_id] = drops_attempt
                # Various metrics
                codes = {
                    "Kicks from hand": "kicks",
                    "Passes": "passes",
                    "Runs": "runs",
                    "Metres run with ball": "meters",
                    "Clean breaks": "breaks",
                    "Defenders beaten": "def_beaten",
                    "Offloads": "offloads",
                    "Turnovers conceded": "turnovers",
                    "Penalties conceded": "pens_conceded",
                }
                if title in ["Kicks from hand", "Passes", "Runs", "Metres run with ball", "Clean breaks", "Defenders beaten", "Offloads", "Turnovers conceded", "Penalties conceded"]:
                    result[codes.get(title)][team_id] = int(value)
                # Rucks both initiated and won
                if title == "Rucks won":
                    rucks_re = regex.match("^\\n([0-9]+) from ([0-9]+)", value)
                    if not rucks_re:
                        continue
                    result["rucks_init"][team_id] = int(rucks_re.captures(2)[0])
                    result["rucks_won"][team_id] = int(rucks_re.captures(1)[0])
                #mauls both initiated and won
                if title == "Mauls won":
                    mall_re = regex.match("^\\n([0-9]+) from ([0-9]+)", value)
                    if not mall_re :
                        continue
                    result["mall_init"][team_id] = int(mall_re.captures(2)[0])
                    result["mall_won"][team_id] = int(mall_re.captures(1)[0])
                #tackles
                if title == "Tackles made/missed":
                    tackles_re = regex.match("^([0-9]+)/([0-9]+)$", value)
                    if not tackles_re :
                        continue
                    result["tackles_made"][team_id] = int(tackles_re.captures(1)[0])
                    result["tackles_missed"][team_id] = int(tackles_re.captures(2)[0])
                #scrums
                if title == "Scrums on own feed":
                    scrums_re = regex.match("^\\n\\t  ([0-9]+) won, ([0-9]+) lost", value)
                    if not scrums_re:
                        continue
                    result["scrums_won_on_feed"][team_id] = int(scrums_re.captures(1)[0])
                    result["scrums_lost_on_feed"][team_id] = int(scrums_re.captures(2)[0])
                #lineouts
                if title == "Lineouts on own throw":
                    lineout_re = regex.match("^\\n\\t  ([0-9]+) won, ([0-9]+) lost", value)
                    if not lineout_re:
                        continue
                    result["lineouts_won_on_throw"][team_id] = int(lineout_re.captures(1)[0])
                    result["lineouts_lost_on_throw"][team_id] = int(lineout_re.captures(2)[0])
                #cards
                if title == "Yellow/red cards":
                    cards_re = regex.match("^([0-9]+)/([0-9]+)$", value)
                    if not cards_re:
                        continue
                    result["yellow_cards"][team_id] = int(cards_re.captures(1)[0])
                    result["red_cards"][team_id] = int(cards_re.captures(2)[0])

            for metric_name, metric_values in result.items():
                yield metric_name, metric_values


    def _parse_player_stats(self, row, potential_team, potential_team_id ):
        """method that parses players match stats from row,
        format : {"match_id" : "placeholder", "player_id" : int, }
        """

        assert type(potential_team) is list, "potential teams must be in a list"
        assert type(potential_team_id) is list, "potential teams id must be in a list"
        assert len(potential_team) == len(potential_team_id) and len(potential_team) == 2, "potential teams and team ids must be of same length 2"

        player_stats = {}
        #getting the player name and deducing his id and his team id
        player_name = row.css("td:nth-child(2)::text")
        if not player_name :
            return None
        player_name = player_name.extract_first()
        try :
            home_player_id = self._get_player_id_from_name(player_name, potential_team[0])
        except RuntimeError:
            home_player_id = None
        try:
            away_player_id = self._get_player_id_from_name(player_name, potential_team[1])
        except RuntimeError:
            away_player_id = None

        if away_player_id and home_player_id:
            return None
        elif not(away_player_id or home_player_id) :
            return None
        elif home_player_id :
            player_id = home_player_id
            team_id = potential_team_id[0]
        elif away_player_id:
            player_id = away_player_id
            team_id = potential_team_id[1]
        else:
            return None
        player_stats["player_id"] = player_id
        player_stats["team_id"] = team_id

        #getting the statistics
        #tries and assists
        tries_assists = row.css("td:nth-child(3)::text")
        if tries_assists:
            tries_assists = tries_assists.extract_first()
            tries_assists_re = regex.match("^([0-9]+)/([0-9])+$", tries_assists)
            if tries_assists_re:
                tries = int(tries_assists_re.captures(1)[0])
                player_stats["tries"] = tries
                assists = int(tries_assists_re.captures(2)[0])
                player_stats["assists"] = assists
        #points
        points = row.css("td:nth-child(4)::text")
        if points:
            points = int(points.extract_first())
            player_stats["points"] = points
        #kicks runs passes
        k_r_p = row.css("td:nth-child(5)::text")
        if k_r_p:
            k_r_p = k_r_p.extract_first()
            k_r_p_re = regex.match("^([0-9]+)/([0-9]+)/([0-9]+)$", k_r_p)
            if k_r_p_re:
                kicks = int(k_r_p_re.captures(1)[0])
                player_stats["kicks"] = kicks
                passes = int(k_r_p_re.captures(2)[0])
                player_stats["passes"] = passes
                runs = int(k_r_p_re.captures(3)[0])
                player_stats["runs"] = runs
        #meters ran
        meters_ran = row.css("td:nth-child(6)::text")
        if meters_ran:
            meters_ran = int(meters_ran.extract_first())
            player_stats["meters"] = meters_ran
        #clean breacks
        breaks = row.css("td:nth-child(7)::text")
        if breaks:
            breaks = int(breaks.extract_first())
            player_stats["breaks"] = breaks
        #defenders beaten
        defenders_beaten = row.css("td:nth-child(8)::text")
        if defenders_beaten:
            defenders_beaten = int(defenders_beaten.extract_first())
            player_stats["def_beaten"] = defenders_beaten
        #offloads
        offloads = row.css("td:nth-child(9)::text")
        if offloads:
            offloads = int(offloads.extract_first())
            player_stats["offloads"] = offloads
        #turnovers
        turnovers = row.css("td:nth-child(10)::text")
        if turnovers:
            turnovers = int(turnovers.extract_first())
            player_stats["turnovers"] = turnovers
        #tackles made and missed
        tackles = row.css("td:nth-child(11)::text")
        if tackles:
            tackles = tackles.extract_first()
            tackles_re = regex.match("^([0-9]+)/([0-9]+)", tackles)
            if tackles_re:
                tackles_made = int(tackles_re.captures(1)[0])
                player_stats["tackles_made"] = tackles_made
                tackles_missed = int(tackles_re.captures(2)[0])
                player_stats["tackles_missed"] = tackles_missed
        #lineouts
        lineouts = row.css("td:nth-child(12)::text")
        if lineouts:
            lineouts = lineouts.extract_first()
            lineouts_re = regex.match("^([0-9]+)/([0-9]+)$", lineouts)
            if lineouts_re:
                lineouts_won_on_throw = int(lineouts_re.captures(1)[0])
                player_stats["lineouts_won_on_throw"] = lineouts_won_on_throw
                lineouts_stolen_from_opp = int(lineouts_re.captures(2)[0])
                player_stats["lineouts_stolen_from_opp"] = lineouts_stolen_from_opp
        #penalties conceded
        pens_conceded = row.css("td:nth-child(13)::text")
        if pens_conceded:
            pens_conceded = int(pens_conceded.extract_first())
            player_stats["pens_conceded"] = pens_conceded
        #cards
        cards = row.css("td:nth-child(14)::text")
        if cards:
            cards = cards.extract_first()
            cards_re = regex.match("^([0-9]+)/([0-9]+)", cards)
            if cards_re:
                player_stats["yellow_cards"] = int(cards_re.captures(1)[0])
                player_stats["red_cards"] = int(cards_re.captures(2)[0])

        return player_stats


    def _match_iframe_parse(self, response):
        """ Main callback that handles the parsing of the match iframe containing most of the data.
        Returns PlayerStats() (enriched) per player, MatchExtraStats() and PlayerExtraStats() if available.
        Redirects to player info page.
        """

        # Get the forwarded match data
        match = response.meta.get('match')

        # Start the actual parsing
        self.logger.info("[{}] Start parsing match data ...".format(match["id"]))

        # 1) Parse the match headline
        tokens = response.xpath("//td[@class=\"liveSubNavText1\"]/text()")
        if not tokens:
            self.logger.error("[{}] Can't extract headline. Skipping match ...".format(match["id"]))
            return
        notes = response.xpath("//td[@class=\"liveTblNotes\"]/a/text()")
        if len(notes) == 2:
            loader = VenueLoader(item = Venue())
            loader.add_value("id", match["ground_id"])
            loader.add_value("name", notes[0].get())
            venue = loader.load_item()
            yield venue

        headlines = "".join([item.rstrip().replace("\n", "") for item in tokens.extract()]).split(" - ")
        if len(headlines) == 2:
            match_stats = {
                "scored": 0,
                "conceded": 0,
            }
            error = False
            for i, headline in enumerate(headlines):
                id = match["home_team_id"] if i == 0 else match["away_team_id"]
                name = regex.findall("([a-zA-Z ]+)", headline)
                score = regex.findall("(\d+)(?!G)", headline)
                if not name or not score:
                    self.logger.error("[{}] Missing data in headline. Skipping match ...".format(match["id"], id))
                    error = True
                    break

                # Create Team item
                loader = TeamLoader(item = Team())
                loader.add_value("id", id)
                loader.add_value("name", name[0].strip())
                team = loader.load_item()
                yield team

                # Store scores in match_stats dict
                score = int(score[0].strip())
                match_stats["scored" if i == 0 else "conceded"] = score

            if not error:
                yield match
            else:
                return

            # Yield partial MatchStats item
            for i, team_id in enumerate([match["home_team_id"], match["away_team_id"]]):
                loader = MatchStatsLoader(item = MatchStats())
                loader.add_value("match_id", match["id"])
                loader.add_value("team_id", team_id)
                loader.add_value("scored", match_stats["scored"] if i == 0 else match_stats["conceded"])
                loader.add_value("conceded", match_stats["conceded"] if i == 0 else match_stats["scored"])
                yield loader.load_item()

        else:
            self.logger.error("[{}] Headline can't be parsed. Skipping match ...".format(match["id"]))
            return

        # 2) Get an array of the tabs indexed by title
        tabs = response.css("#scrumContent .tabbertab")
        if not tabs:
            self.logger.error("[{}] No tabs, aborting.".format(match["id"]))
            return # If no tabs, we have no match info, so drop this request

        tabs = [(tab.css("h2::text").extract_first(), tab) for tab in tabs]
        tabs = { tab[0]: tab[1] for tab in tabs if tab[0]}

        # 3) Get all players in the match from the "Teams" tab. For each team line-up,
        #    - extract player ids from list and creates requests to player page
        #    - extract match specific info and creates requests to player match page

        if "Teams" not in tabs:
            self.logger.error("[{}] No \"Teams\" tab, aborting.".format(match["id"]))
            return # We ain't gonna do nothin' bru
        else:
            self.logger.info("[{}] Found {} tabs : {}".format(match["id"], len(tabs), ", ".join(tabs.keys())))

        # Create players dict to match _parse_teams_score_data inputs
        player_dict = { "home": {}, "away": {}}

        teams = tabs["Teams"].css("table tr:last-child .divTeams")
        if len(teams) < 2:
            # Hmm hmm ...
            return

        for index, team in enumerate(teams):
            # For each team group (first team or replacements)
            for position, group in enumerate(team.xpath("table")):
                # For each player (discard first rows - subtitles)
                players = group.css("tr.liveTblRowWht")[1:]
                for player in players:
                    # Get basic info
                    player_loader = PlayerLoader(item = Player(), response = response, selector = player)
                    player_loader.add_css("id", "a[class^=\"liveLineupText\"]::attr(href)", re = "\/([0-9]+)\.")
                    player_loader.add_css("name", "a[class^=\"liveLineupText\"]::text")
                    player_info = player_loader.load_item()
                    # Discard players without id
                    if not player_info:
                        continue

                    # Go to the player page to scrape it
                    yield response.follow(
                        url = "/statsguru/rugby/player/{}.html".format(player_info["id"]),
                        callback = self.player_info_parse,
                        meta = { "player_info" : player_info }
                    )

                    player_stats_fields = {
                        "number" : "td.liveTblTextGrn::text",
                        "position" : "td.liveTblColCtr::text",
                    }

                    # Get match-specific info for each player
                    player_stats_loader = PlayerStatsLoader(item = PlayerStats(), response = response, selector = player)
                    player_stats_loader.add_value("player_id", player_info["id"])
                    player_stats_loader.add_value("team_id", match["home_team_id"] if index == 0 else match["away_team_id"])
                    player_stats_loader.add_value("match_id", match["id"])
                    player_stats_loader.add_value("first_team", position == 0)
                    for field, selector in player_stats_fields.items():
                        player_stats_loader.add_css(field, selector)
                    player_stats = player_stats_loader.load_item()

                    yield player_stats

                    # Populate player dict for later use
                    if player_info["id"] and player_info["name"]:
                        player_dict["home" if index == 0 else "away"][player_info["id"]] = (player_info.get("name"), player_stats.get("position"), player_stats.get("number"))

        # Abort parsing if we don't have info on players
        if not player_dict["home"] or not player_dict["away"]:
            self.logger.error("[{}] Missing player data in \"Teams\" tab, aborting.".format(match["id"]))
            return
        self.logger.info("[{}] Found {} players for home team ({}) and {} players for away team ({})".format(match["id"], len(player_dict["home"]), match["home_team_id"], len(player_dict["away"]), match["away_team_id"]))

        # 3) Parse top summary of the Teams tab to retrieve the names of the players who scored
        self.logger.info("[{}] Begin score parsing ...".format(match["id"]))
        scores = tabs["Teams"].css(".liveTblScorers")
        if scores and len(scores) > 1:
            # Everything is pretty all right' man
            # For each team (home and away)
            for index in range(2):
                team_scores = defaultdict(lambda: defaultdict(int))
                for score in scores[index::2]:
                    # Extract from html
                    fields = (score.css(".liveTblTextGrn::text").extract_first(), score.css("td::text").extract_first())
                    if not all(fields):
                        self.logger.info("[{}] Skipping score entry, not all fields present. Skipping.", match["id"])
                        continue

                    # Format the parsed data
                    event_type, event_data = [item.rstrip().replace("\n", "") for item in fields]
                    if not event_type.lower() in ["pens", "tries", "drops", "cons"]:
                        # Event type not supported
                        self.logger.info("[{}] Unsupported event \"{}\". Skipping.".format(match["id"], event_type))
                        continue
                    self.logger.info("[{}] Handling event \"{}\" ...".format(match["id"], event_type))
                    if event_data == "none":
                        self.logger.info("[{}] ({}) No data for event. Skipping.".format(match["id"], event_type))
                        continue

                    # Do the regex matching
                    # First, split the event string to get each player separately
                    list_of_events = regex.split("\,(?! \d)", event_data)
                    if not list_of_events:
                        self.logger.warning("[{}] ({}) Can't extract player actions. Skipping.".format(match["id"], event_type))
                        self.logger.debug("String : {}".format(event_data))
                        continue

                    # Cleaning of trailing spaces
                    list_of_events = [item.strip() for item in list_of_events]
                    self.logger.debug(list_of_events)

                    # For each event (corresponding to one player), parse the info
                    # and yield the data structure
                    for event in list_of_events:
                        event_parsed = regex.match("((?:[\w\-\' ](?!\d))+) *([\d])*(?:\((?:(\d+)[, ]*)*\))*", event)
                        if not event_parsed:
                            self.logger.warning("[{}] ({}) Action parsing failed. Skipping.".format(match["id"], event_type))
                            self.logger.debug("String : {}".format(event))
                            continue

                        name = event_parsed.captures(1)
                        occurences = event_parsed.captures(2)
                        times = event_parsed.captures(3)
                        player_id = None

                        if len(name) != 0:
                            name = name[0].strip()
                        else:
                            # Can't do anything without a name bru'
                            continue

                        # Attempt to guess the player id
                        try :
                            player_id = self._get_player_id_from_name(name, player_dict["home" if index == 0 else "away"])
                        except RuntimeError:
                            # Drop game events that can't be associated to a player
                            self.logger.warning("[{}] ({}) Unable to guess player id for \"{}\". Skipping.".format(match["id"], event_type, name))

                        if player_id:
                            if times:
                                for time in times:
                                    # We have some game events to emit
                                    loader = GameEventLoader(item = GameEvent(), response = response)
                                    loader.add_value("player_id", player_id)
                                    loader.add_value("team_id", match["home_team_id"] if index == 0 else match["away_team_id"])
                                    loader.add_value("match_id", match["id"])
                                    loader.add_value("time", time)
                                    loader.add_value("action_type", event_type.lower())
                                    game_event = loader.load_item()
                                    self.logger.info("[{}] ({}) Event : {} ({}) at time {}\"".format(game_event["match_id"], game_event["action_type"], name, game_event["player_id"], game_event["time"]))
                                    yield game_event

                        team_scores[player_id][event_type.lower()] += max(len(occurences)+1, len(times))

                # Once we've processed all the scores for a given team, we yield
                # the corresponding data structures
                for player_id, player_score in team_scores.items():
                    if player_id == None:
                        continue
                    loader = PlayerStatsLoader(item = PlayerStats(), response = response)
                    loader.add_value("player_id", player_id)
                    loader.add_value("team_id", match["home_team_id"] if index == 0 else match["away_team_id"])
                    loader.add_value("match_id", match["id"])
                    for stat_name, stat_value in player_score.items():
                        loader.add_value(stat_name, stat_value)
                    player_stats = loader.load_item()
                    self.logger.info("[{}] Stats for {} : {}".format(match["id"], player_id, player_score))
                    yield player_stats

                # Generate the MatchStats items
                scores_summary = { "tries": 0, "cons": 0, "pens": 0, "drops": 0 }
                for team_score in team_scores.values():
                    for score_category, value in team_score.items():
                        scores_summary[score_category] += value

                loader = MatchStatsLoader(item = MatchStats())
                loader.add_value("match_id", match["id"])
                loader.add_value("team_id", match["home_team_id"] if index == 0 else match["away_team_id"])
                loader.add_value("scored", match_stats["scored"] if index == 0 else match_stats["conceded"])
                loader.add_value("conceded", match_stats["conceded"] if index == 0 else match_stats["scored"])
                for score_category, value in scores_summary.items():
                    loader.add_value(score_category, value)
                yield loader.load_item()

        # 3) Parse the "Match stats" page which provides team-level aggregated statistics
        if "Match stats" in tabs:
            loaders = {
                match["home_team_id"]: MatchExtraStatsLoader(item = MatchExtraStats()),
                match["away_team_id"]: MatchExtraStatsLoader(item = MatchExtraStats())
            }
            # For each metric, add respective values to the loader of the corresponding team
            for metric, scores in self._parse_match_stats(tabs["Match stats"], match):
                for team_id, score in scores.items():
                    loaders[team_id].add_value(metric, score)

            # Then tag the structures with some useful metadata (for the pipeline) before yielding
            for team_id, loader in loaders.items():
                loader.add_value("match_id", match["id"])
                loader.add_value("team_id", team_id)
                yield loader.load_item()

        # 4) If available, parse the "{team} stats" page which provides player-level statistics
        for index, tab in enumerate((tabs[title] for title in tabs.keys() if regex.search("^[a-zA-Z ]+ stats$", title))):
            for player_row in tab.css("table tr") :
                player_stats = self._parse_player_stats(player_row, potential_team = [player_dict["home"], player_dict["away"]], potential_team_id = [match["home_team_id"], match["away_team_id"]])
                if player_stats:
                    loader = PlayerExtraStatsLoader(item = PlayerExtraStats())
                    loader.add_value("match_id", match["id"])
                    for key, value in player_stats.items():
                        loader.add_value(key, value)
                    yield loader.load_item()
