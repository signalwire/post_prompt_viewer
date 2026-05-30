#!/usr/bin/perl
use CGI;
use Data::Dumper;
use JSON;
use DateTime;
use DateTime::TimeZone;
use utf8;
use Encode;

binmode STDOUT, ':encoding(UTF-8)';
binmode STDERR, ':encoding(UTF-8)';

my $cgi    = new CGI;
my $json = JSON->new->allow_nonref;
$json = $json->pretty(1);
my $plain_json = JSON->new->allow_nonref;

print "Content-Type: application/json\n\n";


foreach ($cgi->param) {                                                                                                                                       
    $F{$_} = decode('UTF-8', $cgi->param($_));
}

if ($F{POSTDATA})
  {
    $json_text = $F{POSTDATA};
    $post_data = $json->decode( $json_text );
    $app_name = $post_data->{"app_name"};
  }

print STDERR Dumper ($post_data) . "\n\n";

if ($post_data->{"action"} && $post_data->{"action"} eq "fetch_conversation") {
    if ($post_data->{"conversation_id"}) {
	my $summary = `cat /var/www/html/ai/summary/$post_data->{conversation_id}`;
	my $rj = {};
	if ($summary) {
	    $rj->{response} = "Conversation found";
	    $rj->{"conversation_summary"} = $summary;
	} else {
	    $rj->{response} = "No previous conversation found.\n";
	}
	my $json_str = $plain_json->encode($rj);
	printf STDERR $json_str . "\n\n";
	printf qq#$json_str#;
	exit;
    }
}

if ($post_data->{"conversation_id"} && $post_data->{"conversation_summary"}) {
    my $dt = DateTime->now;    
    my $tz = DateTime::TimeZone->new(name => 'America/Chicago');
    $dt->set_time_zone($tz);
    my $formatted_time = $dt->strftime("%D %I:%M%p CT");

    open L, qq#>>/var/www/html/ai/summary/$post_data->{"conversation_id"}#;
    my $txt = $post_data->{"conversation_summary"};
    chomp($txt);
    print L qq#- Call $formatted_time: $txt\n#;
    close L;
}

$uuid = `/usr/bin/uuid`;
chomp $uuid;

$app_name =~ s/[^\w\d\-]//g;

if ($app_name) {
  open L, ">/var/www/html/ai/logs/${app_name}-${uuid}";
  binmode L, ':encoding(UTF-8)';
  print L $json_text;
  close L;
}

printf qq#{ "response": "data received" }\n#;
